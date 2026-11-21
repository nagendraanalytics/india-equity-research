from datetime import date

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from ier.db.models import (
    Agent,
    AgentEvent,
    Chunk,
    Document,
    EvidenceLink,
    Fact,
    Instrument,
    Job,
    Scorecard,
    Signal,
    Source,
)
from tests.db.conftest import assert_safe_test_db


def _inst(isin="INE001A01036", **kw):
    return Instrument(isin=isin, name="HDFC", nse_code="HDFC", **kw)


def test_pgvector_enabled(session):
    assert session.execute(
        text("select count(*) from pg_extension where extname='vector'")
    ).scalar() == 1


def test_instrument_unique_by_isin(session):
    session.add(_inst())
    session.flush()
    session.add(_inst())
    with pytest.raises(IntegrityError):
        session.flush()


def test_scorecard_versioned_by_run_date(session):
    i = _inst()
    session.add(i)
    session.flush()
    session.add_all(
        [
            Scorecard(instrument_id=i.id, run_date=date(2026, 11, 1), long_term_score=60),
            Scorecard(instrument_id=i.id, run_date=date(2026, 11, 2), long_term_score=65),
        ]
    )
    session.flush()
    assert session.query(Scorecard).filter_by(instrument_id=i.id).count() == 2


def test_evidence_link_rejects_link_without_fact_or_chunk(session):
    src = Source(url="https://nseindia.com/x", type="filing", tier=1)
    session.add(src)
    session.flush()
    session.add(EvidenceLink(claim_type="benefit", source_id=src.id))
    with pytest.raises(IntegrityError):
        session.flush()


def test_source_defaults_and_tier_range(session):
    s = Source(url="u", type="filing", tier=1)
    session.add(s)
    session.flush()
    assert s.status == "ok"
    session.add(Source(url="u2", type="filing", tier=4))
    with pytest.raises(IntegrityError):
        session.flush()


def _inst_flush(session):
    i = _inst()
    session.add(i)
    session.flush()
    return i


@pytest.mark.parametrize("field", ["long_term_score", "short_term_score"])
@pytest.mark.parametrize("bad", [-0.1, 100.1])
def test_score_out_of_range_rejected(session, field, bad):
    i = _inst_flush(session)
    session.add(Scorecard(instrument_id=i.id, run_date=date(2026, 11, 1), **{field: bad}))
    with pytest.raises(IntegrityError):
        session.flush()


def test_score_boundaries_accepted(session):
    i = _inst_flush(session)
    session.add_all(
        [
            Scorecard(instrument_id=i.id, run_date=date(2026, 11, 1), long_term_score=0, short_term_score=100),
            Scorecard(instrument_id=i.id, run_date=date(2026, 11, 2), long_term_score=100, short_term_score=0),
        ]
    )
    session.flush()


def test_confidence_check(session):
    i = _inst_flush(session)
    for n, c in enumerate(["low", "medium", "high"], start=1):
        session.add(Scorecard(instrument_id=i.id, run_date=date(2026, 11, n), confidence=c))
    session.flush()
    session.add(Scorecard(instrument_id=i.id, run_date=date(2026, 12, 1), confidence="certain"))
    with pytest.raises(IntegrityError):
        session.flush()


def test_duplicate_scorecard_run_date_rejected(session):
    i = _inst_flush(session)
    session.add(Scorecard(instrument_id=i.id, run_date=date(2026, 11, 1)))
    session.flush()
    session.add(Scorecard(instrument_id=i.id, run_date=date(2026, 11, 1)))
    with pytest.raises(IntegrityError):
        session.flush()


def test_agent_job_event_signal_defaults_and_fks(session):
    i = _inst_flush(session)
    root = Agent(tier=1, role="orchestrator")
    session.add(root)
    session.flush()
    child = Agent(tier=2, parent_id=root.id, scope_instrument_id=i.id, scope_name="HDFC", role="fundamentals")
    session.add(child)
    session.flush()
    assert child.parent_id == root.id

    j1 = Job(agent_id=child.id, trigger="schedule")
    session.add(j1)
    session.flush()
    assert j1.status == "queued" and j1.attempts == 0
    j2 = Job(agent_id=child.id, trigger="event", depends_on=j1.id)
    session.add(j2)
    session.flush()
    assert j2.depends_on == j1.id

    ev = AgentEvent(agent_id=child.id, kind="started", payload={"a": 1})
    session.add(ev)
    session.flush()
    assert ev.cost == 0

    sc = Scorecard(instrument_id=i.id, run_date=date(2026, 11, 1))
    session.add(sc)
    session.flush()
    sig = Signal(instrument_id=i.id, rating="watch", scorecard_id=sc.id, rules_version="v1", run_date=date(2026, 11, 1))
    session.add(sig)
    session.flush()
    assert sig.id is not None


def test_job_with_unknown_agent_rejected(session):
    session.add(Job(agent_id=999999, trigger="schedule"))
    with pytest.raises(IntegrityError):
        session.flush()


def test_evidence_link_accepts_fact_or_chunk(session):
    i = _inst_flush(session)
    src = Source(url="https://nseindia.com/x", type="filing", tier=1)
    session.add(src)
    session.flush()
    doc = Document(source_id=src.id, instrument_id=i.id)
    fact = Fact(instrument_id=i.id, source_id=src.id, metric="revenue", value=1.0, period="FY25")
    session.add_all([doc, fact])
    session.flush()
    chunk = Chunk(document_id=doc.id, source_id=src.id, text="t")
    session.add(chunk)
    session.flush()
    session.add_all(
        [
            EvidenceLink(claim_type="benefit", fact_id=fact.id, source_id=src.id),
            EvidenceLink(claim_type="risk", chunk_id=chunk.id, source_id=src.id),
        ]
    )
    session.flush()


def test_guard_refuses_non_test_database():
    with pytest.raises(RuntimeError):
        assert_safe_test_db("postgresql+psycopg://postgres@localhost:5433/ier")
    assert_safe_test_db("postgresql+psycopg://postgres@localhost:5433/ier_test")


def test_fact_conflict_defaults_false(session):
    inst = Instrument(isin="INE000000001", name="X")
    src = Source(url="u", type="http", tier=1)
    session.add_all([inst, src])
    session.flush()
    f = Fact(instrument_id=inst.id, source_id=src.id, metric="revenue", value=1.0, period="Q1")
    session.add(f)
    session.flush()
    session.refresh(f)
    assert f.conflict is False


def test_document_sector_column_and_index(session):
    cols = {r[0]: r[1] for r in session.execute(text(
        "select column_name, is_nullable from information_schema.columns "
        "where table_name='documents' and column_name='sector'"))}
    assert cols == {"sector": "YES"}
    idx = session.execute(text(
        "select indexdef from pg_indexes where tablename='documents' "
        "and indexname='ix_documents_sector'")).scalar()
    assert idx is not None and "(sector)" in idx
    src = Source(url="u", type="report", tier=2)
    session.add(src)
    session.flush()
    d = Document(source_id=src.id, instrument_id=None, sector="Information Technology")
    session.add(d)
    session.flush()
    session.refresh(d)
    assert d.sector == "Information Technology" and d.instrument_id is None


def test_job_queue_columns(session):
    cols = {r[0]: (r[1], r[2]) for r in session.execute(text(
        "select column_name, is_nullable, data_type from information_schema.columns "
        "where table_name='jobs' and column_name in ('claimed_at','last_error','payload')"))}
    assert cols == {
        "claimed_at": ("YES", "timestamp with time zone"),
        "last_error": ("YES", "text"),
        "payload": ("YES", "json"),
    }
    a = Agent(tier=5, role="company")
    session.add(a)
    session.flush()
    j = Job(agent_id=a.id, trigger="new_source", payload={"source_id": 1, "content_hash": "h"})
    session.add(j)
    session.flush()
    session.refresh(j)
    assert j.payload == {"source_id": 1, "content_hash": "h"} and j.claimed_at is None
