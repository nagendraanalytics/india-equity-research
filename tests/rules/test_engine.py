from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from ier.db.models import Fact, Instrument, Source
from ier.rules.engine import SUB_SCORES, evaluate

IT_RULES = Path(__file__).resolve().parents[2] / "src" / "ier" / "rules" / "config" / "it.yaml"
_n = 0


def make_instrument(session):
    global _n
    _n += 1
    inst = Instrument(isin=f"INE{_n:09d}", name=f"Rules Co {_n}", macro_sector="Information Technology")
    session.add(inst)
    session.flush()
    return inst


def add_facts(session, inst, values, period_end=date(2026, 3, 31), created_at=None):
    src = Source(url="http://example.test/f", type="filing", tier=1)
    session.add(src)
    session.flush()
    for metric, value in values.items():
        kw = {"created_at": created_at} if created_at else {}
        session.add(Fact(instrument_id=inst.id, source_id=src.id, metric=metric, value=value,
                         period="FY26", period_end=period_end, **kw))
    session.flush()


FULL = {
    "operating_margin": 24.0, "revenue_growth": 12.0, "attrition": 13.0,
    "debt_equity": 0.05, "promoter_pledging": 0.0,
}


def test_scores_within_0_100(session):
    inst = make_instrument(session)
    add_facts(session, inst, {"operating_margin": 900.0, "revenue_growth": -500.0, "attrition": 0.0,
                              "debt_equity": 99.0, "promoter_pledging": 0.0})
    res = evaluate(inst.id, IT_RULES, session)
    assert set(res.sub_scores) == set(SUB_SCORES) and len(SUB_SCORES) == 7
    for v in [*res.sub_scores.values(), res.long_term, res.short_term]:
        assert v is None or 0.0 <= v <= 100.0
    assert res.sub_scores["growth"] == 0.0
    assert res.sub_scores["quality"] == 100.0  # 900% margin and 0 attrition both clamp to 100
    assert res.sub_scores["balance_sheet"] == 0.0


def test_same_facts_same_scores(session):
    inst = make_instrument(session)
    add_facts(session, inst, FULL)
    assert evaluate(inst.id, IT_RULES, session) == evaluate(inst.id, IT_RULES, session)


def test_latest_fact_wins(session):
    inst = make_instrument(session)
    add_facts(session, inst, {"operating_margin": 5.0}, period_end=date(2025, 3, 31))
    add_facts(session, inst, {"operating_margin": 30.0}, period_end=date(2026, 3, 31))
    # same period_end: later created_at wins
    add_facts(session, inst, {"revenue_growth": 1.0}, created_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    add_facts(session, inst, {"revenue_growth": 25.0}, created_at=datetime(2026, 2, 1, tzinfo=timezone.utc))
    res = evaluate(inst.id, IT_RULES, session)
    assert res.sub_scores["quality"] > 50
    assert res.sub_scores["growth"] > 50


def test_industry_median_fact_overrides_benchmark(session):
    inst = make_instrument(session)
    add_facts(session, inst, {"operating_margin": 24.0})
    base = evaluate(inst.id, IT_RULES, session).sub_scores["quality"]
    add_facts(session, inst, {"operating_margin_industry_median": 48.0})
    assert evaluate(inst.id, IT_RULES, session).sub_scores["quality"] < base


def test_high_promoter_pledging_raises_red_flag(session):
    inst = make_instrument(session)
    add_facts(session, inst, {**FULL, "promoter_pledging": 35.0})
    res = evaluate(inst.id, IT_RULES, session)
    assert any("pledg" in f.lower() for f in res.red_flags)
    other = make_instrument(session)
    add_facts(session, other, FULL)
    assert evaluate(other.id, IT_RULES, session).red_flags == []


def test_missing_fact_lowers_confidence_not_score_to_zero(session):
    inst = make_instrument(session)
    add_facts(session, inst, {k: v for k, v in FULL.items() if k != "revenue_growth"})
    res = evaluate(inst.id, IT_RULES, session)
    assert res.sub_scores["growth"] is None
    assert "revenue_growth" in res.missing
    assert res.sub_scores["quality"] is not None
    assert res.long_term is not None and res.long_term > 0
    empty = make_instrument(session)
    res2 = evaluate(empty.id, IT_RULES, session)
    assert res2.long_term is None and res2.short_term is None
    assert all(v is None for v in res2.sub_scores.values())
    assert set(res2.missing) >= set(FULL)


def test_rules_version_recorded(session, tmp_path):
    inst = make_instrument(session)
    assert evaluate(inst.id, IT_RULES, session).rules_version == "it-2026.11.3"
    # the version comes from the file being evaluated, not a constant
    other = tmp_path / "v.yaml"
    other.write_text(IT_RULES.read_text().replace("it-2026.11.3", "custom-9"))
    assert evaluate(inst.id, other, session).rules_version == "custom-9"


def test_no_pledging_fact_no_red_flag(session):
    inst = make_instrument(session)
    add_facts(session, inst, {k: v for k, v in FULL.items() if k != "promoter_pledging"})
    assert evaluate(inst.id, IT_RULES, session).red_flags == []


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_fact_treated_as_missing(session, bad):
    inst = make_instrument(session)
    add_facts(session, inst, {**FULL, "revenue_growth": bad, "promoter_pledging": bad})
    res = evaluate(inst.id, IT_RULES, session)
    assert res.sub_scores["growth"] is None
    assert res.sub_scores["governance"] is None
    assert "revenue_growth" in res.missing
    assert res.red_flags == []


def test_non_finite_median_falls_back_to_benchmark(session):
    inst = make_instrument(session)
    add_facts(session, inst, {"operating_margin": 24.0})
    base = evaluate(inst.id, IT_RULES, session)
    add_facts(session, inst, {"operating_margin_industry_median": float("nan")})
    assert evaluate(inst.id, IT_RULES, session) == base


def write_rules(tmp_path, text):
    p = tmp_path / "r.yaml"
    p.write_text(text)
    return p


RULE = """version: v1
rules:
  - {metric: m, comparison: higher_better, benchmark: %s, weight: %s, horizon: long, sub_score: quality}
"""


def test_zero_benchmark_uses_absolute_difference(session, tmp_path):
    inst = make_instrument(session)
    add_facts(session, inst, {"m": 0.5})
    res = evaluate(inst.id, write_rules(tmp_path, RULE % (0, 1)), session)
    assert res.sub_scores["quality"] == 75.0


def test_empty_rules_file_rejected(session, tmp_path):
    with pytest.raises(ValueError, match="empty"):
        evaluate(1, write_rules(tmp_path, ""), session)


def test_missing_benchmark_rejected(session, tmp_path):
    txt = "version: v1\nrules:\n  - {metric: m, comparison: higher_better, weight: 1, horizon: long, sub_score: quality}\n"
    with pytest.raises(ValueError, match="benchmark"):
        evaluate(1, write_rules(tmp_path, txt), session)


def test_bad_comparison_rejected_without_facts(session, tmp_path):
    txt = (RULE % (1, 1)).replace("higher_better", "sideways")
    with pytest.raises(ValueError, match="comparison"):
        evaluate(999999, write_rules(tmp_path, txt), session)


def test_zero_weight_rejected(session, tmp_path):
    with pytest.raises(ValueError, match="weight"):
        evaluate(1, write_rules(tmp_path, RULE % (1, 0)), session)


def test_unknown_red_flag_operator_rejected(session, tmp_path):
    txt = "version: v1\nred_flags:\n  - {metric: m, operator: approx, threshold: 1, message: x}\n"
    with pytest.raises(ValueError, match="operator"):
        evaluate(1, write_rules(tmp_path, txt), session)


# --- context values (Task 13: sector tailwind) ------------------------------------------

def test_context_used_when_no_fact(session):
    inst = make_instrument(session)
    assert evaluate(inst.id, IT_RULES, session).sub_scores["sector_tailwind"] is None
    res = evaluate(inst.id, IT_RULES, session, context={"sector_tailwind": 75.0})
    assert res.sub_scores["sector_tailwind"] == pytest.approx(75.0)
    assert "sector_tailwind" not in res.missing


def test_fact_takes_precedence_over_context(session):
    inst = make_instrument(session)
    add_facts(session, inst, {"sector_tailwind": 40.0})
    res = evaluate(inst.id, IT_RULES, session, context={"sector_tailwind": 90.0})
    assert res.sub_scores["sector_tailwind"] == pytest.approx(40.0)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), None, "70", True])
def test_bad_context_value_ignored(session, bad):
    inst = make_instrument(session)
    res = evaluate(inst.id, IT_RULES, session, context={"sector_tailwind": bad})
    assert res.sub_scores["sector_tailwind"] is None
    assert "sector_tailwind" in res.missing


def test_sector_tailwind_rule_is_placeholder_long_horizon():
    from ier.rules.engine import load_rules
    cfg = load_rules(IT_RULES)
    rule = next(r for r in cfg["rules"] if r["metric"] == "sector_tailwind")
    assert rule["benchmark"] == 50 and rule["comparison"] == "higher_better"
    assert rule["sub_score"] == "sector_tailwind" and rule["horizon"] == "long"
    assert rule["weight"] < 1.0
    block = IT_RULES.read_text().split("metric: sector_tailwind")[1].split("- metric")[0]
    assert "PLACEHOLDER" in block.split("red_flags")[0]


# --- F1: the latest fact is chosen by reporting period, never by insertion order ----------

def _fact(session, inst, metric, value, period, tier=1, period_end=None):
    src = Source(url=f"http://example.test/{metric}/{period}/{tier}", type="filing", tier=tier)
    session.add(src)
    session.flush()
    session.add(Fact(instrument_id=inst.id, source_id=src.id, metric=metric, value=value,
                     period=period, period_end=period_end))
    session.flush()


def test_prior_year_comparative_listed_after_current_year_does_not_win(session):
    # one results table: current year then the prior-year comparative column, in that order
    inst = make_instrument(session)
    _fact(session, inst, "operating_margin", 30.0, "FY2025")
    _fact(session, inst, "operating_margin", 12.0, "FY2024")
    rr = evaluate(inst.id, IT_RULES, session)
    from ier.rules.engine import load_rules, score_value
    rule = next(r for r in load_rules(IT_RULES)["rules"] if r["metric"] == "operating_margin")
    assert rr.sub_scores["quality"] == pytest.approx(
        score_value(30.0, rule["benchmark"], rule["comparison"]))


def test_stored_period_end_beats_parsed_period(session):
    inst = make_instrument(session)
    _fact(session, inst, "operating_margin", 30.0, "FY2025", period_end=date(2025, 3, 31))
    _fact(session, inst, "operating_margin", 12.0, "odd label", period_end=date(2026, 3, 31))
    rr = evaluate(inst.id, IT_RULES, session)
    assert rr.sub_scores["quality"] < 50  # 12% (the later period_end) is used


def test_fact_without_parseable_period_is_missing_and_noted(session):
    inst = make_instrument(session)
    _fact(session, inst, "operating_margin", 30.0, "latest")
    rr = evaluate(inst.id, IT_RULES, session)
    assert "operating_margin" in rr.missing
    assert rr.sub_scores["quality"] is None
    assert any("no parseable period" in n and "operating_margin" in n for n in rr.notes)


# --- F9: only Tier 1/2 facts are scored ---------------------------------------------------

def test_tier3_fact_raises_no_red_flag_and_is_not_scored(session):
    inst = make_instrument(session)
    _fact(session, inst, "promoter_pledging", 55.0, "FY25", tier=3)
    rr = evaluate(inst.id, IT_RULES, session)
    assert rr.red_flags == []
    assert rr.sub_scores["governance"] is None
    assert "promoter_pledging" in rr.missing


def test_tier3_fact_never_shadows_an_older_tier1_fact(session):
    inst = make_instrument(session)
    _fact(session, inst, "operating_margin", 30.0, "FY2025", tier=1)
    _fact(session, inst, "operating_margin", 1.0, "FY2026", tier=3)
    _fact(session, inst, "revenue_growth", 20.0, "FY2026", tier=2)
    rr = evaluate(inst.id, IT_RULES, session)
    assert rr.sub_scores["quality"] > 50 and rr.sub_scores["growth"] > 50
