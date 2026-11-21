"""Persistence for algorithm scores. Flushes, never commits (the caller owns the transaction)."""
from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from ier.algos.composite import Composite, compute_composite
from ier.algos.core import AlgoResult
from ier.algos.registry import all_algorithms
from ier.algos.runner import load_all
from ier.db.models import AlgoScore, Instrument


def save_results(session: Session, instrument_id: int, as_of: date, results: dict[str, AlgoResult],
                 source_id: int | None, run_id: str | None) -> int:
    existing = {r.algo_id: r for r in session.scalars(
        select(AlgoScore).where(AlgoScore.instrument_id == instrument_id, AlgoScore.as_of == as_of))}
    for algo_id, res in results.items():
        row = existing.get(algo_id)
        if row is None:
            session.add(AlgoScore(instrument_id=instrument_id, algo_id=algo_id, as_of=as_of, score=res.score,
                                  value=res.value, reason=res.reason, source_id=source_id, run_id=run_id))
        else:
            row.score, row.value, row.reason, row.source_id, row.run_id = res.score, res.value, res.reason, source_id, run_id
    session.flush()
    return len(results)


def latest_results(session: Session, instrument_id: int) -> dict[str, AlgoScore]:
    out: dict[str, AlgoScore] = {}
    rows = session.scalars(select(AlgoScore).where(AlgoScore.instrument_id == instrument_id)
                           .order_by(AlgoScore.as_of.desc(), AlgoScore.id.desc()))
    for r in rows:
        out.setdefault(r.algo_id, r)
    return out


def latest_composite(session: Session, instrument_id: int, cfg: dict) -> Composite | None:
    rows = latest_results(session, instrument_id)
    if not rows:
        return None
    load_all()
    enabled = cfg.get("enabled_families")
    algos = [a for a in all_algorithms() if not enabled or a.family in enabled]
    return compute_composite({k: AlgoResult(v.score, v.value, v.reason) for k, v in rows.items()}, algos)


def peer_momentum(session: Session, macro_sector: str) -> dict[str, float]:
    """isin -> latest 6-month return value for companies in the macro sector."""
    out: dict[str, float] = {}
    q = (select(Instrument.isin, AlgoScore.value, AlgoScore.as_of)
         .join(AlgoScore, AlgoScore.instrument_id == Instrument.id)
         .where(Instrument.macro_sector == macro_sector, AlgoScore.algo_id == "mom_6m", AlgoScore.value.is_not(None))
         .order_by(AlgoScore.as_of.desc()))
    for isin, value, _ in session.execute(q):
        out.setdefault(isin, value)
    return out
