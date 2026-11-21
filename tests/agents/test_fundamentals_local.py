"""Fundamentals extraction reading filings from a local folder (no network, empty allowlist)."""
from sqlalchemy import select

from ier.agents import fundamentals
from ier.agents.fundamentals import ExtractedFact, extract_facts
from ier.db.models import Chunk, Instrument, Source
from tests.agents.test_fundamentals import fake_complete
from tests.knowledge.conftest import fake_embed

ISIN = "INE009A01021"
TEXT = "Infosys Limited. Revenue from operations was Rs 41,764 crore for the quarter."


def test_extracts_facts_from_local_file_with_configured_tier(session, tmp_path, monkeypatch):
    inst = Instrument(isin=ISIN, name="Infosys Ltd")
    session.add(inst)
    session.flush()
    d = tmp_path / "filings" / ISIN
    d.mkdir(parents=True)
    (d / "results__Q2FY27__2026-10-16.txt").write_text(TEXT)
    config = {"local_dir": str(tmp_path / "filings"), "local_tier": 1,
              "allowed_hosts": {}, "index_url": ""}

    def items(s):
        c = s.scalars(select(Chunk).where(Chunk.text.contains("Revenue from"))).first()
        return [ExtractedFact(metric="revenue", value=41764, unit="INR crore",
                              period="Q2FY27", chunk_id=c.id)]

    monkeypatch.setattr(fundamentals, "complete", fake_complete(items))
    facts = extract_facts(session, inst.id, "r1", embed=fake_embed, config=config)
    assert [(f.metric, f.value, f.period) for f in facts] == [("revenue", 41764.0, "Q2FY27")]
    src = session.get(Source, facts[0].source_id)
    assert (src.type, src.tier) == ("local", 1) and src.url.startswith("file:")


def test_no_local_files_is_stale_not_an_error(session, tmp_path):
    inst = Instrument(isin=ISIN, name="Infosys Ltd")
    session.add(inst)
    session.flush()
    (tmp_path / "filings").mkdir()
    out = extract_facts(session, inst.id, "r1", embed=fake_embed,
                        config={"local_dir": str(tmp_path / "filings")})
    assert out == []
