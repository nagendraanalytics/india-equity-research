"""Deterministic rules engine: Facts -> sub-scores, horizon scores and red flags. No LLM."""
from __future__ import annotations

import math
import operator
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from ier.db.models import Fact, Source
from ier.rules.periods import parse_period_end

SUB_SCORES = (
    "quality", "growth", "valuation", "balance_sheet", "governance", "sector_tailwind", "momentum",
)
HORIZONS = ("long", "short")
MEDIAN_SUFFIX = "_industry_median"
# Bump whenever scoring CODE changes results (score_value, fact selection, weighting): it is
# part of the company run fingerprint, so unchanged-input skips do not keep stale scores.
# History: 1 = original; 2 = latest fact by period end, Tier 1/2 facts only (final review F1/F9);
# 3 = targeted extraction (rules metric names, aliases) and derived debt_equity/revenue_growth.
SCORING_CODE_VERSION = "3"
MAX_TIER = 2  # only Tier 1/2 sources are evidence; Tier 3 (forums, chatter) is never scored
_OPS = {"gt": operator.gt, "gte": operator.ge, "lt": operator.lt, "lte": operator.le}


@dataclass(frozen=True)
class RuleResult:
    sub_scores: dict[str, float | None]
    long_term: float | None
    short_term: float | None
    red_flags: list[str]
    rules_version: str
    missing: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


_RULE_KEYS = ("metric", "comparison", "benchmark", "weight", "horizon", "sub_score")
_FLAG_KEYS = ("metric", "operator", "threshold", "message")
_COMPARISONS = ("higher_better", "lower_better")


def _validate(cfg: object, path: Path) -> dict:
    if not isinstance(cfg, dict) or not cfg:
        raise ValueError(f"{path}: rules file is empty or not a mapping")
    if "version" not in cfg:
        raise ValueError(f"{path}: missing required key 'version'")
    for i, rule in enumerate(cfg.get("rules") or []):
        name = f"rule #{i} ({rule.get('metric', '?') if isinstance(rule, dict) else '?'})"
        for key in _RULE_KEYS:
            if not isinstance(rule, dict) or key not in rule:
                raise ValueError(f"{path}: {name} missing required key {key!r}")
        if rule["comparison"] not in _COMPARISONS:
            raise ValueError(f"{path}: {name} invalid comparison {rule['comparison']!r}")
        if not isinstance(rule["weight"], (int, float)) or not rule["weight"] > 0:
            raise ValueError(f"{path}: {name} weight must be > 0")
        if rule["sub_score"] not in SUB_SCORES:
            raise ValueError(f"{path}: {name} unknown sub_score {rule['sub_score']!r}")
        if rule["horizon"] not in HORIZONS:
            raise ValueError(f"{path}: {name} unknown horizon {rule['horizon']!r}")
    for i, rf in enumerate(cfg.get("red_flags") or []):
        name = f"red flag #{i} ({rf.get('metric', '?') if isinstance(rf, dict) else '?'})"
        for key in _FLAG_KEYS:
            if not isinstance(rf, dict) or key not in rf:
                raise ValueError(f"{path}: {name} missing required key {key!r}")
        if rf["operator"] not in _OPS:
            raise ValueError(f"{path}: {name} unknown operator {rf['operator']!r}")
    return cfg


def load_rules(path: Path) -> dict:
    with open(path) as f:
        return _validate(yaml.safe_load(f), path)


def _latest(session: Session, instrument_id: int, metric: str,
            undated: set[str] | None = None) -> float | None:
    """Value of the latest Fact for `metric`, by reporting period end.

    period_end is the stored value, else parsed from the period label (ier.rules.periods);
    ties go to the later created_at, then the higher id. Insertion order never decides between
    periods (a results table lists prior-year comparatives next to the current year). Facts
    whose period cannot be dated are ignored: if only such facts exist the metric is missing
    and its name is added to `undated` (when given) so the caller can say so.
    Only facts from Tier 1/2 sources count (MAX_TIER); others are invisible here.
    """
    rows = session.execute(
        select(Fact.value, Fact.period, Fact.period_end, Fact.created_at, Fact.id)
        .join(Source, Source.id == Fact.source_id)
        .where(Fact.instrument_id == instrument_id, Fact.metric == metric,
               Source.tier.is_not(None), Source.tier <= MAX_TIER)
    ).all()
    dated = [(r.period_end or parse_period_end(r.period), r) for r in rows]
    dated = [(end, r) for end, r in dated if end is not None]
    if rows and not dated and undated is not None:
        undated.add(metric)
    if not dated:
        return None
    _, best = max(dated, key=lambda d: (d[0], d[1].created_at, d[1].id))
    value = best.value
    if value is None or not math.isfinite(value):
        return None  # NaN/inf facts are treated as missing, never scored
    return value


def score_value(value: float, benchmark: float, comparison: str) -> float:
    """Map a value to 0-100 against a benchmark (higher score is always better).

    PLACEHOLDER, uncalibrated scale: 50 at the benchmark, +/-50 per 100% relative
    deviation (relative to abs(benchmark)), clamped to [0, 100]. A zero benchmark
    has no relative scale, so the absolute difference is used instead; a negative
    benchmark uses abs(benchmark) as the denominator.
    """
    diff = value - benchmark
    rel = diff / abs(benchmark) if benchmark else diff
    if comparison == "lower_better":
        rel = -rel
    elif comparison != "higher_better":
        raise ValueError(f"unknown comparison {comparison!r}")
    return max(0.0, min(100.0, 50.0 + 50.0 * rel))


def _weighted(pairs: list[tuple[float, float]]) -> float | None:
    total = sum(w for _, w in pairs)
    if not pairs or total <= 0:
        return None
    return sum(s * w for s, w in pairs) / total


def _context_value(context: dict[str, float] | None, metric: str) -> float | None:
    if not context:
        return None
    v = context.get(metric)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return None
    return float(v)


def evaluate(
    instrument_id: int, rules_path: Path, session: Session, *,
    context: dict[str, float] | None = None,
) -> RuleResult:
    """Score one instrument from its Facts.

    `context` (metric -> value) supplies values that are not Facts, e.g. sector_tailwind from
    the sector agent's SectorView.tailwind_input. Lowest precedence: a context value is used
    only when the instrument has no usable (finite) Fact for that metric. Non-numeric, bool
    and non-finite context values are ignored (the metric stays missing).
    """
    cfg = load_rules(rules_path)
    undated: set[str] = set()

    def value_of(metric: str) -> float | None:
        v = _latest(session, instrument_id, metric, undated)
        return _context_value(context, metric) if v is None else v

    by_horizon: dict[str, list[tuple[float, float]]] = {h: [] for h in HORIZONS}
    by_sub = {name: [] for name in SUB_SCORES}
    missing: list[str] = []
    for rule in cfg.get("rules", []):
        sub = rule["sub_score"]
        value = value_of(rule["metric"])
        if value is None:
            missing.append(rule["metric"])
            continue
        median = _latest(session, instrument_id, rule["metric"] + MEDIAN_SUFFIX)
        benchmark = rule["benchmark"] if median is None else median
        score = score_value(value, benchmark, rule["comparison"])
        weight = float(rule["weight"])
        by_sub[sub].append((score, weight))
        by_horizon[rule["horizon"]].append((score, weight))

    red_flags: list[str] = []
    for rf in cfg.get("red_flags", []):
        value = value_of(rf["metric"])
        if value is not None and _OPS[rf["operator"]](value, rf["threshold"]):
            red_flags.append(rf["message"].format(value=value, threshold=rf["threshold"]))

    missing = sorted(set(missing))
    notes = [f"Missing data for: {', '.join(missing)}"] if missing else []
    if undated:
        notes.append("Facts with no parseable period were ignored for: "
                     + ", ".join(sorted(undated)))
    return RuleResult(
        sub_scores={name: _weighted(pairs) for name, pairs in by_sub.items()},
        long_term=_weighted(by_horizon["long"]),
        short_term=_weighted(by_horizon["short"]),
        red_flags=red_flags,
        rules_version=str(cfg["version"]),
        missing=missing,
        notes=notes,
    )
