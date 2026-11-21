"""Knowledge store: chunk, embed and store documents; retrieve one company's or one sector's
evidence.

Retrieval is hybrid: pgvector cosine similarity plus Postgres full-text search, fused with
reciprocal rank fusion (RRF, k=60). Every query is scoped before ranking: `retrieve` filters
by Document.instrument_id so a company agent only ever sees its own company's chunks;
`retrieve_sector` filters by Document.sector AND instrument_id IS NULL, so a sector agent
sees only that sector's own documents (never another sector's, never a company filing).
Both share one exact-scan core; only the scope predicate differs. Chunks keep their source_id (and the
Chunk.source / Chunk.document relationships) so claims can cite their evidence.

Full-text config is 'english' (see migration 0003): stemming lets "revenues" match
"revenue", and numbers such as "4,512" are kept as tokens, so exact figures still match.
"""
from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path

import yaml
from sqlalchemy import Text, and_, cast, func, select
from sqlalchemy.types import UserDefinedType
from sqlalchemy.orm import Session, joinedload

from ier.db.models import EMBEDDING_DIM, Chunk, Document
from ier.knowledge.chunking import chunk_text

ROOT = Path(__file__).resolve().parents[3]
CONFIG_PATH = ROOT / "config" / "knowledge.yaml"
DEFAULTS = {
    "embedding_model": "BAAI/bge-base-en-v1.5",
    "chunk_max_chars": 1200,
    "chunk_overlap_chars": 150,
}
RRF_K = 60
CANDIDATE_FLOOR = 50  # per-ranker candidates considered before fusion

Embedder = Callable[[list[str]], list[list[float]]]



class _TSQuery(UserDefinedType):
    cache_ok = True

    def get_col_spec(self, **kw):
        return "tsquery"


_model = None
_model_lock = threading.Lock()


def load_config(path: Path = CONFIG_PATH) -> dict:
    cfg = dict(DEFAULTS)
    if path.exists():
        with open(path) as f:
            cfg.update(yaml.safe_load(f) or {})
    return cfg


def _default_embedder(texts: list[str]) -> list[list[float]]:
    """Embed with the configured sentence-transformers model, loaded on first use."""
    global _model
    with _model_lock:
        if _model is None:
            from sentence_transformers import SentenceTransformer  # heavy; import lazily

            _model = SentenceTransformer(load_config()["embedding_model"])
    vectors = _model.encode(texts, normalize_embeddings=True)
    return [list(map(float, v)) for v in vectors]


def _embed(embed: Embedder | None, texts: list[str]) -> list[list[float]]:
    vectors = (embed or _default_embedder)(texts)
    if len(vectors) != len(texts) or any(len(v) != EMBEDDING_DIM for v in vectors):
        raise ValueError(f"embedder must return {len(texts)} vectors of {EMBEDDING_DIM} dims")
    return vectors


def ingest_document(
    session: Session, source_id: int, text: str, meta: dict, *, embed: Embedder | None = None
) -> int:
    """Chunk and embed `text`, storing a Document plus Chunk rows. Returns chunks stored.

    meta keys: exactly one of instrument_id (company document) or sector (sector document,
    stored with instrument_id NULL); title, doc_type, period. Flushes, never commits.
    Blank text stores nothing.
    """
    has_inst = meta.get("instrument_id") is not None
    sector = meta.get("sector")
    has_sector = isinstance(sector, str) and bool(sector.strip())
    if has_inst == has_sector or (sector is not None and not has_sector):
        raise ValueError(
            "exactly one of meta['instrument_id'] or a non-blank meta['sector'] is required")
    cfg = load_config()
    pieces = chunk_text(text or "", cfg["chunk_max_chars"], cfg["chunk_overlap_chars"])
    if not pieces:
        return 0
    vectors = _embed(embed, pieces)
    doc = Document(
        source_id=source_id,
        instrument_id=meta.get("instrument_id"),
        sector=sector if has_sector else None,
        title=meta.get("title"),
        doc_type=meta.get("doc_type"),
        period=meta.get("period"),
    )
    session.add(doc)
    session.flush()
    session.add_all(
        Chunk(document_id=doc.id, source_id=source_id, ordinal=i, text=p, embedding=v)
        for i, (p, v) in enumerate(zip(pieces, vectors))
    )
    session.flush()
    return len(pieces)


def retrieve(
    session: Session, query: str, instrument_id: int, k: int = 8, *, embed: Embedder | None = None
) -> list[Chunk]:
    """Up to k chunks of this instrument's documents, best first (RRF of vector + full text)."""
    if instrument_id is None:
        raise ValueError("instrument_id is required")
    return _search(session, query, Document.instrument_id == instrument_id, k, embed)


def retrieve_sector(
    session: Session, query: str, sector: str, k: int = 8, *, embed: Embedder | None = None
) -> list[Chunk]:
    """Up to k chunks of this sector's own documents (exact sector match, no company filings)."""
    if not isinstance(sector, str) or not sector.strip():
        raise ValueError("sector is required")
    return _search(
        session, query, and_(Document.sector == sector, Document.instrument_id.is_(None)), k, embed)


def _search(session: Session, query: str, scope, k: int, embed: Embedder | None) -> list[Chunk]:
    """Hybrid RRF search over chunks whose Document matches `scope` (applied before ranking)."""
    if k <= 0 or not query.strip():
        return []
    pool = max(k * 5, CANDIDATE_FLOOR)
    qvec = _embed(embed, [query])[0]
    on = Chunk.document_id == Document.id

    # Exact scan on purpose: no ANN (HNSW/ivfflat) index. An approximate global top-N would be
    # filtered by scope afterwards and could silently drop a company's or sector's best matches.
    vec_ids = session.scalars(
        select(Chunk.id).join(Document, on).where(scope, Chunk.embedding.is_not(None))
        .order_by(Chunk.embedding.cosine_distance(qvec), Chunk.id).limit(pool)
    ).all()

    # plainto_tsquery ANDs terms; swap & for | so any matching term counts. Chunks that match
    # every term (e.g. an exact figure plus its label) sort ahead of partial matches.
    all_terms = func.plainto_tsquery("english", query)
    tsq = cast(func.replace(cast(all_terms, Text), "&", "|"), _TSQuery())
    fts_ids = session.scalars(
        select(Chunk.id).join(Document, on).where(scope, Chunk.text_tsv.op("@@")(tsq))
        .order_by(
            Chunk.text_tsv.op("@@")(all_terms).desc(),  # chunks containing every term first
            func.ts_rank_cd(Chunk.text_tsv, tsq).desc(),
            Chunk.id,
        )
        .limit(pool)
    ).all()

    score: dict[int, float] = {}
    for ranking in (vec_ids, fts_ids):
        for rank, cid in enumerate(ranking, start=1):
            score[cid] = score.get(cid, 0.0) + 1.0 / (RRF_K + rank)
    top = sorted(score, key=lambda cid: (-score[cid], cid))[:k]
    if not top:
        return []
    rows = session.scalars(
        select(Chunk).where(Chunk.id.in_(top)).options(joinedload(Chunk.document), joinedload(Chunk.source))
    ).all()
    by_id = {c.id: c for c in rows}
    return [by_id[cid] for cid in top]
