"""Per-sector rule sets and extraction topics (IT default, pharma = NSE macro sector Healthcare)."""
from ier.agents.fundamentals import ALIASES, PHARMA_TOPICS, TOPICS
from ier.db.models import Instrument
from ier.graph import company as cg
from ier.rules.engine import load_rules


def _inst(session, macro, isin):
    i = Instrument(isin=isin, name=macro + " co", macro_sector=macro, sector=macro,
                   industry="x", basic_industry="x", classification_status="classified")
    session.add(i)
    session.flush()
    return i


def test_healthcare_uses_pharma_rules_and_topics_others_use_it(session):
    ph = _inst(session, "Healthcare", "INE000000P01")
    it = _inst(session, "Information Technology", "INE000000I01")
    assert cg.rules_path_for(session, ph.id).name == "pharma.yaml"
    assert cg.rules_path_for(session, it.id).name == "it.yaml"
    assert cg.topics_for(session, ph.id) is PHARMA_TOPICS
    assert cg.topics_for(session, it.id) is TOPICS


def test_pharma_rules_load_and_have_pharma_metrics_not_attrition():
    cfg = load_rules(cg.RULES_BY_MACRO_SECTOR["Healthcare"])
    metrics = {r["metric"] for r in cfg["rules"]}
    assert {"rnd_intensity", "usfda_observations", "operating_margin", "debt_equity"} <= metrics
    assert "attrition" not in metrics
    assert all(r["benchmark"] for r in cfg["rules"])


def test_pharma_topics_ask_for_rnd_and_usfda_and_not_attrition():
    text = " ".join(f for f, _, _ in PHARMA_TOPICS)
    assert "rnd_intensity" in text and "usfda_observations" in text and "attrition" not in text
    assert ALIASES["r&d_intensity"] == "rnd_intensity"


def test_fingerprint_changes_with_rules_file(session):
    ph = _inst(session, "Healthcare", "INE000000P02")
    a = cg.fingerprint(session, ph.id, [], None, cg.RULES_PATH)
    b = cg.fingerprint(session, ph.id, [], None, cg.rules_path_for(session, ph.id))
    assert a != b


NEW_SECTORS = {
    "Financial Services": "financials.yaml", "Fast Moving Consumer Goods": "fmcg.yaml",
    "Consumer Discretionary": "consumer_discretionary.yaml", "Energy": "energy.yaml",
    "Commodities": "commodities.yaml", "Industrials": "industrials.yaml",
    "Telecommunication": "telecom.yaml", "Utilities": "utilities.yaml",
}


def test_every_supported_sector_has_loadable_rules_and_topics_for_each_rule_metric(session):
    import re
    DERIVED = {"debt_equity", "revenue_growth", "sector_tailwind", "price_vs_sma200", "momentum_3m"}
    for n, (macro, fname) in enumerate(NEW_SECTORS.items()):
        inst = _inst(session, macro, f"INE0000S{n:04d}")
        assert cg.rules_path_for(session, inst.id).name == fname
        cfg = load_rules(cg.rules_path_for(session, inst.id))
        assert all(r["benchmark"] for r in cfg["rules"])
        text = " ".join(f for f, _, _ in cg.topics_for(session, inst.id))
        asked = set(re.findall(r"[a-z_]+", text))
        for r in cfg["rules"]:
            m = r["metric"]
            assert m in DERIVED or m in asked or m == "operating_margin", (macro, m)
    # banks are not asked for borrowings: debt/equity means nothing for them
    fin = cg.SECTOR_SETUP["Financial Services"][1]
    assert not any("total_borrowings" in f for f, _, _ in fin)


def test_unmapped_macro_sector_is_refused_not_scored_with_it_rules(session):
    import pytest
    inst = _inst(session, "Diversified", "INE0000D0001")
    with pytest.raises(cg.UnsupportedSector):
        cg.rules_path_for(session, inst.id)
    with pytest.raises(cg.UnsupportedSector):
        cg.topics_for(session, inst.id)
