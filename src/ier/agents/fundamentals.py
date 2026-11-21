"""Fundamentals analyst: extracts cited facts from exchange filings. No scores, facts only.

Flow: list filings -> fetch (tier 1 Source) -> ingest -> retrieve relevant chunks -> LLM
(`extract` role, Pydantic schema) -> Facts. A fact is saved only if the chunk it cites resolves
to a Source row. Facts are idempotent per (instrument, metric, period, value, source), and
facts that disagree on value for the same instrument/metric/period are all kept and flagged.
"""
from __future__ import annotations

import logging
import math
import re
from datetime import date
from decimal import Decimal

import httpx
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ier.agents.untrusted import UNTRUSTED_NOTE, excerpts_block, neutralise
from ier.db.models import Chunk, Document, EvidenceLink, Fact, Instrument, RunLog, Source
from ier.knowledge.retrieval import Embedder, ingest_document, retrieve
from ier.llm.router import complete
from ier.rules.periods import parse_period_end
from ier.tools.base import fetch
from ier.tools.local_filings import ingest_local
from ier.tools import exchange_filings
from ier.tools.exchange_filings import FilingRef, list_filings

log = logging.getLogger(__name__)
MAX_TIER = 2  # same bar as the claim verifier: only Tier 1 or 2 may support anything
QUERY = "revenue, operating margin, net profit, EBITDA, EPS for the quarter or year"
TOP_K = 8
# One search and one model call per topic, so each metric the scoring rules need is asked for
# and retrieved on its own (a single fixed search only ever surfaced the results table).
# (focus text for the prompt, search query, chunks). Metric names are the rules' own names.
TOPICS = (
    ("Metrics: revenue, operating_margin (percent), net_profit, ebitda, eps (per share). "
     "Also revenue_growth (percent, year on year) only if the text states that percentage.",
     QUERY, 6),
    ("Metric: attrition (percent, annualised or trailing twelve months, as the filing states it).",
     "employee attrition rate percent trailing twelve months", 3),
    ("Metrics: total_borrowings and total_equity (balance sheet; same unit and period), and "
     "debt_equity (ratio) only if the text states a debt to equity ratio. If a filing says it "
     "has no borrowings, give total_borrowings 0.",
     "total borrowings debt shareholders equity total equity debt to equity ratio balance sheet", 4),
    ("Metric: promoter_pledging (percent of the promoter holding that is pledged or "
     "encumbered; 0 if the text says nil).",
     "promoter shareholding shares pledged or otherwise encumbered percentage of promoter holding", 3),
)
# Pharma (NSE macro sector "Healthcare"): the same revenue/margin, balance-sheet and pledging
# topics, plus R&D spend and USFDA inspection outcomes in place of attrition.
PHARMA_TOPICS = (
    TOPICS[0],
    ("Metric: rnd_intensity (research and development expense as a percent of revenue/sales, "
     "only if the filing states that percentage).",
     "research and development expenditure percent of sales R&D spend", 3),
    ("Metric: usfda_observations (number of USFDA Form 483 observations in the period; "
     "0 if the text says an inspection closed with no observations). Only count what the "
     "text states.",
     "USFDA inspection Form 483 observations warning letter establishment inspection report", 3),
    TOPICS[2], TOPICS[3],
)
# Names models tend to use -> the rules' metric names.
ALIASES = {
    "operating_margin_percent": "operating_margin", "operating_profit_margin": "operating_margin",
    "ebit_margin": "operating_margin", "attrition_rate": "attrition",
    "employee_attrition": "attrition", "attrition_percent": "attrition",
    "debt_to_equity": "debt_equity", "debt_equity_ratio": "debt_equity",
    "debt_to_equity_ratio": "debt_equity", "promoter_pledged_percent": "promoter_pledging",
    "pledged_shares_percent": "promoter_pledging", "promoter_pledge": "promoter_pledging",
    "r&d_intensity": "rnd_intensity", "rd_intensity": "rnd_intensity", "r_and_d_intensity": "rnd_intensity",
    "rnd_percent": "rnd_intensity", "rnd_to_sales": "rnd_intensity", "r&d_percent_of_sales": "rnd_intensity",
    "form_483_observations": "usfda_observations", "usfda_483_observations": "usfda_observations",
    "fda_observations": "usfda_observations",
    "revenue_growth_yoy": "revenue_growth", "yoy_revenue_growth": "revenue_growth",
}


class ExtractedFact(BaseModel):
    metric: str
    value: float
    unit: str | None = None
    period: str
    # ISO date (YYYY-MM-DD) the period ends on, if the excerpt states it; else parsed from period
    period_end: str | None = None
    chunk_id: int  # id shown as [chunk N] in the prompt; the evidence for this value


class Extraction(BaseModel):
    facts: list[ExtractedFact]


_neutralise = neutralise  # kept as a module-level name (tests and callers use it)


def _prompt(chunks: list[Chunk], focus: str | None = None) -> str:
    return (
        "Extract reported financial figures from the filing excerpts below. "
        + (focus or "Metrics: revenue, operating_margin, net_profit, ebitda, eps.") + " "
        "Use these exact snake_case metric names and only numbers stated in the text. "
        "For each, give metric, numeric value, unit (the currency and scale as written, e.g. "
        "'INR crore', 'USD million', 'percent', 'ratio'), period (e.g. Q2FY27 or FY2025), "
        "period_end (the date the period ends, YYYY-MM-DD, e.g. 2026-09-30 for Q2FY27), and "
        "the chunk_id of the excerpt it came from. The period is the one in that value's own "
        "column or sentence: never relabel a column's period. A results table often lists "
        "prior-period comparative columns: give each value its own period and period_end. If a "
        "table shows both USD and INR, or consolidated and standalone, give each its own unit "
        "and prefer consolidated; never mix columns. Skip anything the text does not state. "
        "Do not compute or score anything.\n"
        + UNTRUSTED_NOTE + "\n\n" + excerpts_block(chunks)
    )


def _number_forms(value: float) -> set[str]:
    """Plain renderings of |value| as they could appear in text (commas already stripped)."""
    out = set()
    v = Decimal(repr(abs(value)))
    # as stated; x100 (crore figure printed in lakh); /100 (lakh figure printed in crore)
    for d in (v, v * 100, v / 100):
        txt = format(d.normalize(), "f")
        out.add(txt)
    return out


def _value_in_text(value: float, text: str) -> bool:
    """True if the value's digits appear in `text` as a standalone number.

    Accepted: commas as thousands separators ("41,764"; Indian "4,17,640" too), a trailing
    percent sign or unit word (crore/lakh/%) next to the number, negatives written with a
    minus or in parentheses (we compare the absolute value), the value as stated, the value
    x100 (crore value printed in lakh), and the value /100 (lakh value printed in crore).
    Anything else is rejected.
    """
    plain = text.replace(",", "")
    for form in _number_forms(value):
        if re.search(rf"(?<![\d.]){re.escape(form)}(?!\d|\.\d)", plain):
            return True
    return False


def _period_end(item: ExtractedFact):
    """The model's ISO period_end if valid, else the end date parsed from the period label;
    None if neither works (the rules engine then ignores the fact and says so)."""
    if item.period_end:
        try:
            return date.fromisoformat(item.period_end.strip())
        except ValueError:
            pass
    return parse_period_end(item.period)


def _stale(session: Session, run_id: str, instrument_id: int, reason: str) -> list[Fact]:
    session.add(RunLog(
        run_id=run_id, outputs={"stale": True, "instrument_id": instrument_id, "reason": reason}
    ))
    session.flush()
    return []


class _FilingSkipped(Exception):
    """Raised from _ingest when a filing's new text was not stored after fetch ran.

    fetch has already advanced the Source content hash; raising makes the per-filing savepoint
    roll that back, so the next run still sees the content as changed and retries.
    """


def _ingest(session: Session, instrument_id: int, ref: FilingRef, embed, fetch_config, client,
            hosts: dict[str, int] | None, local_cfg: dict | None = None) -> bool:
    """Fetch and (re)ingest one filing; True if its text is in the store afterwards.

    Changed content replaces the old Document/Chunks for this source and instrument. Facts
    extracted from the superseded text are deleted with it (unless an EvidenceLink points at
    them), so a restatement is not reported as a conflict with its own earlier version.
    """
    # source tier comes from the host's allowlist entry, never from this caller
    if ref.url.startswith("file:"):
        res = ingest_local(ref.url, session, local_dir=local_cfg["local_dir"],
                           tier=int(local_cfg.get("local_tier", 2)),
                           max_bytes=int(local_cfg.get("local_max_mb", 60)) * 1024 * 1024)
    else:
        res = fetch(ref.url, None, session, client=client, config=fetch_config,
                    allowed_hosts=hosts)
    if not res.content:
        if res.changed:  # empty body that advanced the hash: undo it
            raise _FilingSkipped(f"empty content for {ref.url}")
        return False
    old_docs = session.scalars(
        select(Document).where(
            Document.source_id == res.source_id, Document.instrument_id == instrument_id
        )
    ).all()
    if old_docs and not res.changed:
        return True
    if old_docs:
        ids = [d.id for d in old_docs]
        old_chunk_ids = session.scalars(select(Chunk.id).where(Chunk.document_id.in_(ids))).all()
        old_fact_ids = session.scalars(select(Fact.id).where(
            Fact.instrument_id == instrument_id, Fact.source_id == res.source_id
        )).all()
        # Evidence guard. EvidenceLink.chunk_id is an FK to chunks, so a cited chunk cannot be
        # deleted. If ANY old chunk is cited we change nothing for this filing: old Document,
        # Chunks and Facts stay intact, the filing is logged and skipped (raises _FilingSkipped;
        # the savepoint rolls back so the Source hash is not advanced).
        cited_chunks = session.scalars(select(EvidenceLink.id).where(
            EvidenceLink.chunk_id.in_(old_chunk_ids))).first() if old_chunk_ids else None
        if cited_chunks is not None:
            raise _FilingSkipped(
                f"not re-ingesting {ref.url}: old chunks are cited by evidence links")
        # Chunks are uncited, so replace them. Facts cited via EvidenceLink.fact_id are kept
        # (the link must stay valid); every other old fact from this source is deleted.
        cited_facts = set(session.scalars(select(EvidenceLink.fact_id).where(
            EvidenceLink.fact_id.in_(old_fact_ids))).all()) if old_fact_ids else set()
        session.execute(delete(Chunk).where(Chunk.document_id.in_(ids)))
        session.execute(delete(Document).where(Document.id.in_(ids)))
        doomed = [i for i in old_fact_ids if i not in cited_facts]
        if doomed:
            session.execute(delete(Fact).where(Fact.id.in_(doomed)))
        session.flush()
    src = session.get(Source, res.source_id)
    if src.publish_date is None and ref.published_date:
        src.publish_date = ref.published_date
    stored = ingest_document(
        session, res.source_id, res.content.decode("utf-8", errors="replace"),
        {"instrument_id": instrument_id, "title": f"{ref.filing_type} {ref.period}",
         "doc_type": ref.filing_type, "period": ref.period},
        embed=embed,
    )
    if stored == 0:
        raise _FilingSkipped(f"no text stored for {ref.url}")
    return True


def _flag_conflicts(session: Session, instrument_id: int, keys: set[tuple[str, str]]) -> None:
    for metric, period in keys:
        rows = session.scalars(
            select(Fact).where(
                Fact.instrument_id == instrument_id, Fact.metric == metric, Fact.period == period
            )
        ).all()
        conflict = len({f.value for f in rows}) > 1
        for f in rows:
            f.conflict = conflict


def _save_fact(session: Session, instrument_id: int, item: ExtractedFact,
               by_id: dict, saved: list[Fact]) -> None:
    """Store one extracted value if it is cited, from a Tier 1/2 source, and present in its chunk."""
    chunk = by_id.get(item.chunk_id)
    source = session.get(Source, chunk.source_id) if chunk is not None else None
    if source is None:
        return  # unresolvable evidence: never store an uncited fact
    if source.tier is None or source.tier > MAX_TIER:
        return  # Tier 3 (forums, chatter) is never evidence, so it never becomes a Fact
    if not _value_in_text(item.value, chunk.text):
        return  # value not present in the cited text: likely hallucinated or injected
    if not item.metric.strip() or not item.period.strip() or not math.isfinite(item.value):
        return
    metric = item.metric.strip().lower().replace(" ", "_")
    metric = ALIASES.get(metric, metric)
    fact = session.scalars(select(Fact).where(
        Fact.instrument_id == instrument_id, Fact.metric == metric,
        Fact.period == item.period, Fact.value == item.value,
        Fact.source_id == chunk.source_id,
    )).first()
    if fact is None:
        fact = Fact(
            instrument_id=instrument_id, source_id=chunk.source_id, metric=metric,
            value=item.value, unit=item.unit, period=item.period,
            period_end=_period_end(item),
        )
        session.add(fact)
        session.flush()
    elif fact.period_end is None:  # stored before period_end was extracted
        fact.period_end = _period_end(item)
    if fact not in saved:
        saved.append(fact)


def _derive_facts(session: Session, instrument_id: int, saved: list[Fact]) -> list[Fact]:
    """debt_equity and revenue_growth worked out in code (never by the model) from stated
    facts of one source. Skipped when the inputs have no unit, disagree (conflict) or do not
    line up: debt_equity needs borrowings and equity of the same source, period and unit;
    revenue_growth needs two revenue values of the same source and unit whose periods end
    360 to 370 days apart. The result keeps the source of its inputs."""
    out: list[Fact] = []

    def add(source_id, metric, value, unit, period, period_end):
        exists = session.scalars(select(Fact).where(
            Fact.instrument_id == instrument_id, Fact.source_id == source_id,
            Fact.metric == metric, Fact.period == period)).first()
        if exists is None:
            f = Fact(instrument_id=instrument_id, source_id=source_id, metric=metric,
                     value=round(value, 4), unit=unit, period=period, period_end=period_end)
            session.add(f)
            session.flush()
            out.append(f)

    ok = [f for f in saved if f.unit and not f.conflict]
    by: dict[tuple, dict[str, Fact]] = {}
    for f in ok:
        by.setdefault((f.source_id, f.period, f.unit), {})[f.metric] = f
    for (sid, period, unit), m in by.items():
        b, e = m.get("total_borrowings"), m.get("total_equity")
        if b and e and e.value > 0 and b.value >= 0:
            add(sid, "debt_equity", b.value / e.value, "ratio", period, e.period_end)
    rev: dict[tuple, list[Fact]] = {}
    for f in ok:
        if f.metric == "revenue" and f.period_end is not None:
            rev.setdefault((f.source_id, f.unit), []).append(f)
    for (sid, unit), fs in rev.items():
        for new in fs:
            for old in fs:
                gap = (new.period_end - old.period_end).days
                if 360 <= gap <= 370 and old.value > 0:
                    add(sid, "revenue_growth", (new.value / old.value - 1) * 100, "percent",
                        new.period, new.period_end)
    return out


def extract_facts(
    session: Session,
    instrument_id: int,
    run_id: str,
    *,
    client: httpx.Client | None = None,
    embed: Embedder | None = None,
    config: dict | None = None,
    fetch_config: dict | None = None,
    topics: tuple | None = None,
) -> list[Fact]:
    """Extract cited Facts for one instrument.

    Returns [] and writes a stale RunLog (outputs.reason: "no filings listed", "filings listed
    but none could be ingested", or "no relevant text retrieved") when nothing can be extracted.
    A fact is saved only if its chunk resolves to a Source and its value's digits appear in that
    chunk's text (see _value_in_text); otherwise it is dropped.

    The caller owns the transaction. complete() may raise SchemaError or BudgetExceeded; this
    function does not catch them, so the caller should roll back (or discard the session's
    pending work) as appropriate. Each filing is ingested inside a savepoint, so one failing
    filing is logged and skipped without losing the others.
    """
    inst = session.get(Instrument, instrument_id)
    if inst is None:
        raise ValueError(f"unknown instrument_id {instrument_id}")
    refs = list_filings(
        inst.isin, session=session, client=client, config=config, fetch_config=fetch_config
    )
    if not refs:
        return _stale(session, run_id, instrument_id, "no filings listed")
    cfg = {**exchange_filings.load_config(), **(config or {})}
    local_cfg = cfg if cfg.get("local_dir") else None
    hosts = None if local_cfg else exchange_filings.allowed_hosts(cfg)

    ingested = 0
    for ref in refs:
        try:
            with session.begin_nested():
                ingested += _ingest(session, instrument_id, ref, embed, fetch_config, client,
                                    hosts, local_cfg)
        except _FilingSkipped as exc:  # savepoint rolled back, hash not advanced
            log.warning("%s", exc)
        except Exception:  # per-filing boundary: log and move on
            log.exception("skipping filing %s", ref.url)
    if not ingested:
        return _stale(session, run_id, instrument_id, "filings listed but none could be ingested")
    saved: list[Fact] = []
    any_chunks = False
    for focus, query, k in (topics or TOPICS):
        chunks = retrieve(session, query, instrument_id, k=k, embed=embed)
        if not chunks:
            continue
        any_chunks = True
        by_id = {c.id: c for c in chunks}
        out = complete("extract", _prompt(chunks, focus), Extraction, run_id, session=session)
        for item in out.facts:
            _save_fact(session, instrument_id, item, by_id, saved)
    if not any_chunks:
        return _stale(session, run_id, instrument_id, "no relevant text retrieved")
    _flag_conflicts(session, instrument_id, {(f.metric, f.period) for f in saved})
    saved.extend(_derive_facts(session, instrument_id, saved))   # after flags: skips conflicts
    _flag_conflicts(session, instrument_id, {(f.metric, f.period) for f in saved})
    session.flush()
    return saved


def purge_tier3_facts(session: Session, *, apply: bool = False) -> dict:
    """Facts whose Source tier is above MAX_TIER (or unknown), stored before Tier 3 facts were
    refused. Dry run unless `apply`: then deletes them, except facts an EvidenceLink cites
    (deleting those would break a scorecard's evidence; they are reported, and the rules engine
    ignores them anyway). Flushes, never commits. Returns {"found", "deleted", "kept_cited"}."""
    ids = list(session.scalars(
        select(Fact.id).join(Source, Source.id == Fact.source_id)
        .where((Source.tier.is_(None)) | (Source.tier > MAX_TIER)).order_by(Fact.id)))
    cited = set(session.scalars(select(EvidenceLink.fact_id).where(
        EvidenceLink.fact_id.in_(ids)))) if ids else set()
    doomed = [i for i in ids if i not in cited]
    if apply and doomed:
        session.execute(delete(Fact).where(Fact.id.in_(doomed)))
        session.flush()
    return {"found": len(ids), "deleted": len(doomed) if apply else 0,
            "kept_cited": len(cited)}
