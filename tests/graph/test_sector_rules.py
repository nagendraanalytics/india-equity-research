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
