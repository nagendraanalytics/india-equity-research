"""Algorithm registry."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from ier.algos.core import AlgoResult, ChartSpec, Ctx


@dataclass(frozen=True)
class Algorithm:
    id: str
    name: str
    family: str
    horizon: str  # "long" | "short"
    min_history: int
    description: str
    compute: Callable[[Ctx], AlgoResult]
    chart: Callable[[Ctx], ChartSpec] | None = None
    band: tuple[float, float] | None = None  # PLACEHOLDER default (value scoring 0, value scoring 100)


_REGISTRY: dict[str, Algorithm] = {}


def register(algo: Algorithm) -> Algorithm:
    if algo.id in _REGISTRY:
        raise ValueError(f"algorithm already registered: {algo.id}")
    _REGISTRY[algo.id] = algo
    return algo


def all_algorithms() -> list[Algorithm]:
    return list(_REGISTRY.values())


def get(algo_id: str) -> Algorithm:
    return _REGISTRY[algo_id]
