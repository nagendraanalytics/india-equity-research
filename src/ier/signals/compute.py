"""Signal rules: scorecard + rule result -> research label (spec section 5).

Labels are research labels only: no order, quantity, price or side is ever produced. The
mapping is deterministic and uses only thresholds from rules/config/signals.yaml.
"""
from __future__ import annotations

import math
from datetime import date
from pathlib import Path

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from ier.db.models import Scorecard, Signal
from ier.rules.engine import RuleResult

RATINGS = ("accumulate", "hold", "reduce", "avoid", "insufficient evidence")
INSUFFICIENT = "insufficient evidence"
# best to worst; the red-flag cap compares positions in this order
_ORDER = ("accumulate", "hold", "reduce", "avoid")
CONFIDENCES = ("low", "medium", "high")
DEFAULT_PATH = Path(__file__).resolve().parents[1] / "rules" / "config" / "signals.yaml"


def _num(section: dict, key: str, path, where: str) -> float:
    v = section.get(key)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise ValueError(f"{path}: {where}.{key} must be a finite number")
    return float(v)


def _section(cfg: dict, name: str, path) -> dict:
    s = cfg.get(name)
    if not isinstance(s, dict):
        raise ValueError(f"{path}: missing section {name!r}")
    return s


def validate_config(cfg: object, path: object = "signals config") -> dict:
    """Raise ValueError naming the offending key; return `cfg` unchanged when valid."""
    if not isinstance(cfg, dict) or not cfg:
        raise ValueError(f"{path}: signals file is empty or not a mapping")
    if "version" not in cfg or cfg["version"] in (None, ""):
        raise ValueError(f"{path}: missing required key 'version'")
    gate = _section(cfg, "confidence_gate", path)
    levels = gate.get("insufficient_confidence")
    if not isinstance(levels, list) or not all(x in CONFIDENCES for x in levels):
        raise ValueError(
            f"{path}: confidence_gate.insufficient_confidence must be a list of "
            f"{'/'.join(CONFIDENCES)}")
    w = _section(cfg, "weights", path)
    wl, ws = _num(w, "long", path, "weights"), _num(w, "short", path, "weights")
    if wl < 0 or ws < 0:
        raise ValueError(f"{path}: weights must be >= 0")
    if wl + ws <= 0:
        raise ValueError(f"{path}: weights must have a positive sum")
    b = _section(cfg, "bands", path)
    a, h, r = (_num(b, k, path, "bands") for k in ("accumulate_min", "hold_min", "reduce_min"))
    if not a > h > r:
        raise ValueError(
            f"{path}: bands must be strictly ordered: accumulate_min > hold_min > reduce_min")
    cap = _section(cfg, "red_flag_cap", path).get("max_rating")
    if cap not in _ORDER[1:]:
        raise ValueError(
            f"{path}: red_flag_cap.max_rating must be one of {', '.join(_ORDER[1:])}")
    return cfg


def load_signal_rules(path: Path = DEFAULT_PATH) -> dict:
    with open(path) as f:
        return validate_config(yaml.safe_load(f), path)


def _score(v: float | None) -> float | None:
    if v is None or isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return None
    return float(v)


def _band(score: float, bands: dict) -> str:
    if score >= bands["accumulate_min"]:
        return "accumulate"
    if score >= bands["hold_min"]:
        return "hold"
    if score >= bands["reduce_min"]:
        return "reduce"
    return "avoid"


def compute_signal(scorecard: Scorecard, rule_result: RuleResult, rules_path: Path) -> Signal:
    """Pure: map the scorecard and rule result to an unpersisted Signal.

    `signal.reasons` is a list of plain research-language sentences explaining the decision.
    """
    cfg = load_signal_rules(rules_path)
    reasons: list[str] = []
    rating = _decide(scorecard, rule_result, cfg, reasons)
    sig = Signal(instrument_id=scorecard.instrument_id, rating=rating,
                 scorecard_id=scorecard.id, rules_version=str(cfg["version"]),
                 run_date=scorecard.run_date)
    sig.reasons = reasons
    return sig


def _decide(scorecard: Scorecard, rr: RuleResult, cfg: dict, reasons: list[str]) -> str:
    conf = scorecard.confidence
    # Fail closed: only an exact, listed-as-sufficient confidence value passes the gate.
    if conf is None:
        reasons.append("Scorecard confidence is not available; no other label is considered.")
        return INSUFFICIENT
    if conf not in CONFIDENCES:
        reasons.append("Scorecard confidence value is not recognised; no other label is considered.")
        return INSUFFICIENT
    if conf in cfg["confidence_gate"]["insufficient_confidence"]:
        reasons.append(f"Scorecard confidence is {conf}, which is below the evidence gate; "
                       "no other label is considered.")
        return INSUFFICIENT
    long_s, short_s = _score(rr.long_term), _score(rr.short_term)
    if long_s is None and short_s is None:
        reasons.append("Neither a long-term nor a short-term score is available.")
        return INSUFFICIENT
    clamped = any(v < 0 or v > 100 for v in (long_s, short_s) if v is not None)
    long_s = None if long_s is None else min(100.0, max(0.0, long_s))
    short_s = None if short_s is None else min(100.0, max(0.0, short_s))
    w = cfg["weights"]
    if long_s is None:
        combined = short_s
        reasons.append(f"Only the short-term score ({short_s:.1f}) is available, so it is used alone.")
    elif short_s is None:
        combined = long_s
        reasons.append(f"Only the long-term score ({long_s:.1f}) is available, so it is used alone.")
    else:
        total = w["long"] + w["short"]
        combined = (long_s * w["long"] + short_s * w["short"]) / total
        reasons.append(
            f"Combined score {combined:.1f} from long-term {long_s:.1f} (weight {w['long']}) "
            f"and short-term {short_s:.1f} (weight {w['short']}).")
    if clamped:
        reasons.append("Score out of range, clamped to 0-100 before use.")
    rating = _band(combined, cfg["bands"])
    reasons.append(f"Score {combined:.1f} falls in the {rating} band.")
    flags = [str(f) for f in (rr.red_flags or [])]
    if flags:
        cap = cfg["red_flag_cap"]["max_rating"]
        # Red-flag text is quoted verbatim from the rules engine (not reworded here).
        listed = "; ".join(str(f) for f in flags)
        if _ORDER.index(rating) < _ORDER.index(cap):
            reasons.append(f"Red flag cap applied: the label is limited to {cap} because of: {listed}.")
            rating = cap
        else:
            reasons.append(f"Red flags are present ({listed}); the {rating} label is already "
                           f"at or below the cap of {cap}.")
    return rating


def persist_signal(session: Session, scorecard: Scorecard, signal: Signal) -> Signal:
    """Store `signal`, replacing any Signal for the same scorecard (or the same instrument and
    run date), so a same-day scorecard rewrite never leaves a stale signal. Flushes, never commits."""
    stale = session.scalars(select(Signal).where(
        (Signal.scorecard_id == scorecard.id)
        | ((Signal.instrument_id == scorecard.instrument_id)
           & (Signal.run_date == scorecard.run_date)))).all()
    for old in stale:
        if old is not signal:
            session.delete(old)
    session.flush()
    signal.scorecard_id = scorecard.id
    signal.instrument_id = scorecard.instrument_id
    signal.run_date = scorecard.run_date
    session.add(signal)
    session.flush()
    return signal


def signal_hook(session: Session, instrument_id: int, scorecard_id: int) -> None:
    """Default graph hook: compute and store the Signal for a freshly written scorecard."""
    card = session.get(Scorecard, scorecard_id)
    if card is None or card.instrument_id != instrument_id:
        raise ValueError(f"scorecard {scorecard_id!r} not found for instrument {instrument_id!r}")
    rr = RuleResult(
        sub_scores=dict(card.sub_scores or {}), long_term=card.long_term_score,
        short_term=card.short_term_score, red_flags=list(card.red_flags or []),
        rules_version=card.rules_version or "", missing=[])
    persist_signal(session, card, compute_signal(card, rr, DEFAULT_PATH))
