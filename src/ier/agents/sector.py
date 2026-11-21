"""Sector and industry agents.

`analyse_sector` drafts sector drivers and risks with the `reason` model from the sector's own
documents (Document.sector set, instrument_id NULL), then keeps only items that pass the shared
claim verifier in sector scope: cited chunks exist, belong to that sector's documents, come
from Tier 1/2 sources (none Tier 3/unknown), wording passes the denylist, and the text is
supported by the cited chunks. Checked here first: direction (-1|0|+1), risks never +1, and
evidence ids limited to the excerpts actually shown. Repeated items count once.

`tailwind_input` is computed deterministically from the verified items only:
clamp(50 + 10 * sum(severity * direction), 0, 100), or None when nothing survives. The model
never supplies it; any number it adds to its output is ignored. Nothing is written to Facts
here (wiring the tailwind into company rules is a later task).

`rank_industry` ranks one industry's active members (exact Instrument.industry match) by the
long-term score on each member's LATEST scorecard. Research output, not advice.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ier.agents.untrusted import UNTRUSTED_NOTE, excerpts_block, neutralise
from ier.agents.verifier import verify
from ier.db.models import Instrument, Scorecard
from ier.knowledge.retrieval import Embedder, retrieve_sector
from ier.llm.router import complete

QUERY = ("sector demand, policy, regulation, input costs, pricing, competition, capacity, "
         "credit, exports, risks, outlook")
TOP_K = 10
DIRECTIONS = (-1, 0, 1)
ACTIVE = "active"  # Instrument.status default
_MINUS_RE = re.compile(r"[\u2212\u2013](?=\d)")  # unicode minus / en dash before a digit
_THOUSANDS_RE = re.compile(r"(?<=\d),(?=\d)")
# Number punctuation is kept (decimal point, percent, sign); any other punctuation is removed.
_PUNCT_RE = re.compile(r"((?<=\d)\.(?=\d)|(?<=\d)%|[+\-](?=\d))|[^\w\s]")


@dataclass(frozen=True)
class SectorItem:
    kind: str  # "driver" | "risk"
    text: str
    severity: int  # 1-5
    direction: int  # -1 | 0 | +1
    evidence_ids: tuple[int, ...] = ()  # Chunk ids of sector documents


@dataclass(frozen=True)
class SectorView:
    sector: str
    drivers: tuple[SectorItem, ...] = ()
    risks: tuple[SectorItem, ...] = ()
    tailwind_input: float | None = None
    dropped: tuple[tuple[SectorItem, str], ...] = ()  # (item, reason)


class DraftItem(BaseModel):
    # Lenient types on purpose: a bad value must reach the checks and drop only that item.
    text: str
    severity: Any
    direction: Any
    evidence_ids: list[int] = []


class SectorDraft(BaseModel):
    # Extra keys (a tailwind number, scores) are ignored by Pydantic and never read.
    drivers: list[DraftItem]
    risks: list[DraftItem]


def _prompt(sector: str, chunks) -> str:
    return (
        f"List the main drivers and risks for the Indian equity sector "
        f"'{neutralise(sector)}', using only the sector document excerpts below. Each item has: "
        "text (one plain factual sentence), severity (integer 1-5, how much it matters), "
        "direction (integer: +1 helps the sector, -1 hurts it, 0 neutral or unclear), and "
        "evidence_ids (the chunk_id numbers of the excerpts that state it). Describe facts "
        "only: never give orders, instructions or advice to the reader, and never score or "
        "rate the sector.\n"
        + UNTRUSTED_NOTE + "\n\n" + excerpts_block(chunks)
    )


def _valid_direction(d) -> bool:
    return isinstance(d, int) and not isinstance(d, bool) and d in DIRECTIONS


def tailwind(items) -> float | None:
    """clamp(50 + 10 * sum(severity * direction), 0, 100); None for no items."""
    items = list(items)
    if not items:
        return None
    return float(min(100, max(0, 50 + 10 * sum(i.severity * i.direction for i in items))))


def _norm_text(text: str) -> str:
    """The duplicate key: lowercase, whitespace collapsed, punctuation stripped EXCEPT where it
    is part of a number ('2.5' != '25', '+5%' != '-5%'); thousands commas inside numbers are
    removed ('1,000' == '1000') and a unicode minus before a digit reads as '-'."""
    t = _THOUSANDS_RE.sub("", _MINUS_RE.sub("-", (text or "").lower()))
    return " ".join(_PUNCT_RE.sub(lambda m: m.group(1) or "", t).split())


def _precheck(item: SectorItem, shown: set[int]) -> tuple[SectorItem | None, str | None]:
    """Local checks before the verifier: direction, risk sign, evidence among shown excerpts."""
    if not _valid_direction(item.direction):
        return None, f"invalid direction {item.direction!r}; expected -1, 0 or +1"
    if item.kind == "risk" and item.direction == 1:
        return None, "risk cannot have positive direction"
    if item.evidence_ids:
        ids = tuple(i for i in item.evidence_ids if i in shown)
        if not ids:
            return None, "no cited chunk is among the sector excerpts shown to the model"
        item = replace(item, evidence_ids=ids)
    return item, None


def analyse_sector(
    session: Session, sector: str, run_id: str, *, embed: Embedder | None = None
) -> SectorView:
    """Verified drivers and risks for `sector`. Empty view (no model call) if it has no text.

    A non-string or blank `sector` raises ValueError. Before verification, each item's
    evidence ids are cut down to the chunks actually shown to the model (dropped if none
    remain), invalid directions and risks with direction +1 are dropped. After verification,
    repeated items (same kind and normalised text) keep only the first copy; the others are
    dropped as "duplicate" so they cannot inflate the tailwind. SchemaError from the drafting
    call and BudgetExceeded propagate to the caller.
    """
    if not isinstance(sector, str) or not sector.strip():
        raise ValueError("sector must be a non-blank string")
    chunks = retrieve_sector(session, QUERY, sector, k=TOP_K, embed=embed)
    if not chunks:
        return SectorView(sector=sector)
    shown = {c.id for c in chunks}
    out = complete("reason", _prompt(sector, chunks), SectorDraft, run_id, session=session)
    items = [
        SectorItem(kind=kind, text=d.text, severity=d.severity, direction=d.direction,
                   evidence_ids=tuple(dict.fromkeys(d.evidence_ids)))
        for kind, drafts in (("driver", out.drivers), ("risk", out.risks))
        for d in drafts
    ]
    dropped, to_verify = [], []
    for raw in items:
        item, reason = _precheck(raw, shown)
        if reason is None:
            to_verify.append(item)
        else:
            dropped.append((raw, reason))
    checked = verify(to_verify, session, sector=sector, run_id=run_id)
    dropped += checked.dropped
    kept, seen = [], set()
    for item in checked.kept:
        key = (item.kind, _norm_text(item.text))
        if key in seen:
            dropped.append((item, "duplicate"))
        else:
            seen.add(key)
            kept.append(item)
    return SectorView(
        sector=sector,
        drivers=tuple(i for i in kept if i.kind == "driver"),
        risks=tuple(i for i in kept if i.kind == "risk"),
        tailwind_input=tailwind(kept),
        dropped=tuple(dropped),
    )


def rank_industry(session: Session, industry: str) -> list[tuple[int, float]]:
    """(instrument id, long-term score) for this industry's members, best first.

    Uses each member's latest scorecard (run_date desc, id desc); a member whose latest
    scorecard has no long-term score, or that has none, is left out (no stale fallback).
    Only active members (Instrument.status == "active") are ranked. Ties sort by instrument
    id. Unknown or empty industry: [].
    """
    if not isinstance(industry, str) or not industry:
        return []
    rn = func.row_number().over(
        partition_by=Scorecard.instrument_id,
        order_by=(Scorecard.run_date.desc(), Scorecard.id.desc()),
    ).label("rn")
    latest = (
        select(Scorecard.instrument_id, Scorecard.long_term_score.label("score"), rn)
        .join(Instrument, Instrument.id == Scorecard.instrument_id)
        .where(Instrument.industry == industry, Instrument.status == ACTIVE)
        .subquery()
    )
    rows = session.execute(
        select(latest.c.instrument_id, latest.c.score)
        .where(latest.c.rn == 1, latest.c.score.is_not(None))
        .order_by(latest.c.score.desc(), latest.c.instrument_id)
    ).all()
    return [(iid, float(score)) for iid, score in rows]
