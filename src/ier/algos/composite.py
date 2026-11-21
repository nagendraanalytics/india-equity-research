"""Combine per-algorithm scores into long and short composites (equal weights; PLACEHOLDER)."""
from __future__ import annotations

from dataclasses import dataclass

from ier.algos.core import AlgoResult
from ier.algos.registry import Algorithm

PENDING_FAMILY = "value_quality"  # phase 2: registered but not yet computable, left out of coverage counts


@dataclass(frozen=True)
class Composite:
    long: float | None
    short: float | None
    confidence: float
    agreement: float | None
    n_available: int
    n_total: int


def _mean(xs: list[float]) -> float | None:
    return round(sum(xs) / len(xs), 2) if xs else None


def compute_composite(results: dict[str, AlgoResult], algos: list[Algorithm]) -> Composite:
    live = [a for a in algos if a.family != PENDING_FAMILY]
    scored = [(a, results[a.id].score) for a in live if a.id in results and results[a.id].score is not None]
    long_ = _mean([s for a, s in scored if a.horizon == "long"])
    short = _mean([s for a, s in scored if a.horizon == "short"])
    n_total = len(live)
    agreement = None
    if scored:
        up = sum(1 for _, s in scored if s >= 50)
        agreement = max(up, len(scored) - up) / len(scored)
    return Composite(long_, short, len(scored) / n_total if n_total else 0.0, agreement, len(scored), n_total)
