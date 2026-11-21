import pytest

from ier.db.models import EMBEDDING_DIM, Chunk, Instrument, Source
from ier.knowledge import retrieval
from ier.knowledge.retrieval import ingest_document, retrieve, retrieve_sector
from tests.knowledge.conftest import fake_embed

_n = 0


def inst(session, name="Alpha Ltd"):
    global _n
    _n += 1
    i = Instrument(isin=f"INE{_n:09d}", name=name)
    session.add(i)
    session.flush()
    return i


def src(session, url="https://example.com/a"):
    s = Source(url=url, type="filing", tier=1)
    session.add(s)
    session.flush()
    return s


def ingest(session, source, instrument, text, **meta):
    meta = {"instrument_id": instrument.id, "title": "Annual report", "doc_type": "annual_report",
            "period": "FY2025", **meta}
    return ingest_document(session, source.id, text, meta, embed=fake_embed)


def test_chunks_carry_metadata(session):
    a, s = inst(session), src(session)
    n = ingest(session, s, a, "Revenue grew on strong export demand.\n\nMargins expanded.")
    assert n >= 1
    res = retrieve(session, "export demand revenue", a.id, embed=fake_embed)
    assert res
    c = res[0]
    assert c.source_id == s.id and c.source.url == s.url
    assert c.document.title == "Annual report"
    assert c.document.doc_type == "annual_report"
    assert c.document.period == "FY2025"
    assert c.document.instrument_id == a.id


def test_retrieval_only_returns_requested_company(session):
    a, b = inst(session, "A"), inst(session, "B")
    ingest(session, src(session), a, "Alpha plans a new plant in Gujarat.")
    ingest(session, src(session, "https://example.com/b"), b, "Beta plans a new plant in Gujarat.")
    res = retrieve(session, "new plant Gujarat", a.id, embed=fake_embed)
    assert res and all(c.document.instrument_id == a.id for c in res)


def test_retrieval_filters_by_instrument_even_when_other_company_matches_better(session):
    a, b = inst(session, "A"), inst(session, "B")
    ingest(session, src(session), a, "Alpha sells widgets.")
    ingest(session, src(session, "https://example.com/b"), b,
           "Quarterly lithium battery capacity expansion lithium battery capacity.")
    res = retrieve(session, "lithium battery capacity expansion", a.id, embed=fake_embed)
    assert [c.document.instrument_id for c in res] == [a.id]
    assert retrieve(session, "lithium battery", inst(session, "C").id, embed=fake_embed) == []


def test_hybrid_matches_exact_term(session):
    a = inst(session)
    s = src(session)
    ingest(session, s, a, "Order book order book trends: the order book was discussed at length.")
    ingest(session, s, a, "Order book stood at 7,893 crore at year end.")
    ingest(session, s, a, "Order book of 1,234 crore; order book order book growth.")
    res = retrieve(session, "order book 7,893", a.id, k=3, embed=fake_embed)
    assert len(res) == 3
    assert "7,893" in res[0].text


def test_empty_text_creates_no_chunks(session):
    a, s = inst(session), src(session)
    before = session.query(Chunk).count()
    assert ingest(session, s, a, "   \n\n ") == 0
    assert session.query(Chunk).count() == before


def test_ingest_requires_instrument_id(session):
    s = src(session)
    with pytest.raises(ValueError, match="instrument_id"):
        ingest_document(session, s.id, "text", {"title": "x"}, embed=fake_embed)


def test_k_limits_results(session):
    a, s = inst(session), src(session)
    for i in range(5):
        ingest(session, s, a, f"Dividend note number {i} about payout.")
    assert len(retrieve(session, "dividend payout", a.id, k=3, embed=fake_embed)) == 3


def test_default_embedder_is_lazy():
    assert retrieval._model is None  # nothing loaded at import time


def test_vector_recall_for_small_company_in_large_corpus(session):
    """B's best vector match is weaker than ALL 45 of A's chunks and shares no words with
    the query (so full-text cannot rescue it): only an exact per-company scan finds it."""
    import math

    def vec(*pairs):
        v = [0.0] * EMBEDDING_DIM
        for i, x in pairs:
            v[i] = x
        return v

    def embed(texts):  # hand-placed vectors keyed on text prefix; cosine to query in comments
        out = []
        for t in texts:
            if t.startswith("query"):
                out.append(vec((0, 1.0)))  # the query axis
            elif t.startswith("alpha"):
                out.append(vec((0, 0.9), (1, math.sqrt(1 - 0.81))))  # cos 0.9 to query
            elif t.startswith("beta target"):
                out.append(vec((0, 0.5), (2, math.sqrt(1 - 0.25))))  # cos 0.5 to query
            else:
                out.append(vec((3, 1.0)))  # orthogonal to query
        return out

    a, b = inst(session, "A"), inst(session, "B")
    sa, sb = src(session), src(session, "https://example.com/b")
    for i in range(45):
        ingest_document(session, sa.id, f"alpha filing {i}", {"instrument_id": a.id}, embed=embed)
    for t in ("beta target note", "beta unrelated one", "beta unrelated two"):
        ingest_document(session, sb.id, t, {"instrument_id": b.id}, embed=embed)
    res = retrieve(session, "query terms", b.id, k=3, embed=embed)
    assert res[0].text == "beta target note"
    assert all(c.document.instrument_id == b.id for c in res)


def test_retrieve_requires_instrument_id(session):
    with pytest.raises(ValueError, match="instrument_id"):
        retrieve(session, "anything", None, embed=fake_embed)


def test_ingest_sector_document(session):
    s = src(session)
    n = ingest_document(session, s.id, "Cement demand from housing.",
                        {"sector": "Construction Materials", "title": "note"}, embed=fake_embed)
    assert n == 1
    res = retrieve_sector(session, "cement demand", "Construction Materials", embed=fake_embed)
    assert [c.document.sector for c in res] == ["Construction Materials"]
    assert res[0].document.instrument_id is None
    with pytest.raises(ValueError):
        ingest_document(session, s.id, "x", {"sector": "A", "instrument_id": inst(session).id},
                        embed=fake_embed)
    with pytest.raises(ValueError):
        retrieve_sector(session, "cement", None, embed=fake_embed)


def test_sector_retrieval_isolation_and_recall(session):
    """The target sector's best match is weaker than ALL 45 chunks of another sector and of
    instrument-scoped documents (one even tagged with the same sector) and shares no words
    with the query: only an exact, sector-scoped scan finds it."""
    import math

    from ier.db.models import Document

    def vec(*pairs):
        v = [0.0] * EMBEDDING_DIM
        for i, x in pairs:
            v[i] = x
        return v

    def embed(texts):
        out = []
        for t in texts:
            if t.startswith("query"):
                out.append(vec((0, 1.0)))
            elif t.startswith(("other", "company")):
                out.append(vec((0, 0.9), (1, math.sqrt(1 - 0.81))))  # cos 0.9
            elif t.startswith("target best"):
                out.append(vec((0, 0.5), (2, math.sqrt(1 - 0.25))))  # cos 0.5
            else:
                out.append(vec((3, 1.0)))  # orthogonal
        return out

    s = src(session)
    for i in range(45):
        ingest_document(session, s.id, f"other sector note {i}", {"sector": "Banks"}, embed=embed)
    a = inst(session, "A")
    for i in range(10):
        ingest_document(session, s.id, f"company filing {i}", {"instrument_id": a.id}, embed=embed)
    # an instrument document that also carries the sector tag must still be excluded
    d = Document(source_id=s.id, instrument_id=a.id, sector="Cement")
    session.add(d)
    session.flush()
    session.add(Chunk(document_id=d.id, source_id=s.id, ordinal=0, text="company tagged",
                      embedding=embed(["company tagged"])[0]))
    session.flush()
    for t in ("target best item", "target filler one", "target filler two"):
        ingest_document(session, s.id, t, {"sector": "Cement"}, embed=embed)
    res = retrieve_sector(session, "query terms", "Cement", k=3, embed=embed)
    assert res[0].text == "target best item"
    assert all(c.document.sector == "Cement" and c.document.instrument_id is None for c in res)
    assert retrieve_sector(session, "query terms", "Unknown", embed=embed) == []
    # instrument retrieval never sees sector documents
    assert all(c.document.instrument_id == a.id
               for c in retrieve(session, "query terms", a.id, k=20, embed=embed))
