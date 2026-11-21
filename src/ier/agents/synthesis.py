"""Synthesis: draft benefit/risk claims with the model, then build the scorecard.

The model only drafts cited claims. Every score, sub-score and red flag on the scorecard is
copied from the RuleResult; nothing the model returns can change them. Claims reach the
scorecard only after `verify` (see verifier.py). Signals here are research labels, not advice.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ier.algos.blend import blend_scores
from ier.agents.untrusted import UNTRUSTED_NOTE, excerpts_block
from ier.agents.verifier import VerifiedClaims, load_config
from ier.db.models import Chunk, EvidenceLink, Scorecard
from ier.knowledge.retrieval import Embedder, retrieve
from ier.llm.router import complete
from ier.rules.engine import RuleResult

QUERY = "growth, profitability, margins, debt, governance, risks, litigation, guidance, outlook"
TOP_K = 10


@dataclass(frozen=True)
class Claim:
    kind: str  # "benefit" | "risk"
    text: str
    severity: int  # 1-5
    horizon: str  # "short" | "long"
    evidence_ids: list[int] = field(default_factory=list)  # Chunk ids


class DraftClaim(BaseModel):
    # Deliberately lenient (str/int): invalid values reach the verifier, which drops them.
    kind: str
    text: str
    severity: Any  # not int: a bad value ("high", 3.5) must reach the verifier, not fail the batch
    horizon: str
    evidence_ids: list[int] = []


class Draft(BaseModel):
    claims: list[DraftClaim]  # extra keys (e.g. scores) are ignored by Pydantic and never read


def _prompt(chunks: list[Chunk], rule_result: RuleResult) -> str:
    flags = "; ".join(rule_result.red_flags) or "none"
    return (
        "Write short, factual benefit and risk claims about this company, using only the "
        "filing excerpts below. Each claim has: kind ('benefit' or 'risk'), text (one plain "
        "factual sentence), severity (integer 1-5), horizon ('short' or 'long'), and "
        "evidence_ids (the chunk_id numbers of the excerpts that state it). Describe facts "
        "only: never give orders, instructions or advice to the reader, and never score or "
        "rate the company.\n"
        f"Red flags already found by rules (for context only): {flags}\n"
        + UNTRUSTED_NOTE + "\n\n" + excerpts_block(chunks)
    )


def draft_claims(
    session: Session, instrument_id: int, rule_result: RuleResult, run_id: str,
    *, embed: Embedder | None = None,
) -> list[Claim]:
    """Draft claims from this instrument's own chunks. [] if it has no retrievable text."""
    if instrument_id is None:
        raise ValueError("instrument_id is required")
    chunks = retrieve(session, QUERY, instrument_id, k=TOP_K, embed=embed)
    if not chunks:
        return []
    out = complete("synthesise", _prompt(chunks, rule_result), Draft, run_id, session=session)
    return [
        Claim(kind=c.kind, text=c.text, severity=c.severity, horizon=c.horizon,
              evidence_ids=list(dict.fromkeys(c.evidence_ids)))
        for c in out.claims
    ]


def _confidence(distinct_chunks: int, none_count: int, cfg: dict) -> str:
    c = cfg["confidence"]
    if distinct_chunks < c["min_evidence_chunks"] or none_count > c["max_none_sub_scores"]:
        return "low"
    if distinct_chunks >= c["high_evidence_chunks"] and none_count <= c["high_max_none_sub_scores"]:
        return "high"
    return "medium"


def _item(c: Claim) -> dict:
    return {"text": c.text, "severity": c.severity, "horizon": c.horizon,
            "evidence_ids": list(c.evidence_ids)}


def build_scorecard(
    session: Session, instrument_id: int, rule_result: RuleResult, verified: VerifiedClaims,
    run_date: date, *, config: dict | None = None, algo=None, algo_weight: float = 0.0,
) -> Scorecard:
    """Persist one Scorecard for (instrument, run_date); a same-day rerun updates that row in place
    (same id, so Signals pointing at it stay valid) and recreates its EvidenceLinks.

    Sub-scores and red flags come only from `rule_result` (None stays None). The final long/short
    scores are the rules scores blended with the algorithm composite `algo` at `algo_weight`
    (capped at 0.30; weight 0 or no composite leaves the rules scores exactly as they were).
    Flushes, never commits.
    """
    cfg = config or load_config()
    kept = verified.kept
    distinct = {i for c in kept for i in c.evidence_ids}
    none_count = sum(1 for v in rule_result.sub_scores.values() if v is None)

    card = session.scalars(select(Scorecard).where(
        Scorecard.instrument_id == instrument_id, Scorecard.run_date == run_date)).first()
    fields = dict(
        long_term_score=blend_scores(rule_result.long_term, algo.long if algo else None, algo_weight),
        short_term_score=blend_scores(rule_result.short_term, algo.short if algo else None, algo_weight),
        rules_long_score=rule_result.long_term, rules_short_score=rule_result.short_term,
        algo_long_score=algo.long if algo else None, algo_short_score=algo.short if algo else None,
        algo_weight=float(algo_weight),
        confidence=_confidence(len(distinct), none_count, cfg),
        sub_scores=dict(rule_result.sub_scores),
        benefits=[_item(c) for c in kept if c.kind == "benefit"],
        risks=[_item(c) for c in kept if c.kind == "risk"],
        red_flags=list(rule_result.red_flags),
        rules_version=rule_result.rules_version,
    )
    if card is None:
        card = Scorecard(instrument_id=instrument_id, run_date=run_date, **fields)
        session.add(card)
    else:
        # Update in place: signals.scorecard_id references this row, so it must keep its id.
        for k, v in fields.items():
            setattr(card, k, v)
        session.execute(delete(EvidenceLink).where(EvidenceLink.scorecard_id == card.id))
    session.flush()
    source_of = dict(session.execute(
        select(Chunk.id, Chunk.source_id).where(Chunk.id.in_(distinct))).all()) if distinct else {}
    counters = {"benefit": 0, "risk": 0}
    for c in kept:
        ref = f"{c.kind}:{counters[c.kind]}"
        counters[c.kind] += 1
        for cid in dict.fromkeys(c.evidence_ids):
            session.add(EvidenceLink(
                scorecard_id=card.id, claim_type=c.kind, claim_ref=ref, chunk_id=cid,
                source_id=source_of.get(cid)))
    session.flush()
    return card
