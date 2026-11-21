"""Shared types for the algorithm library."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any


@dataclass(frozen=True)
class Bar:
    day: date
    close: float
    volume: float | None = None


@dataclass(frozen=True)
class AlgoResult:
    score: float | None
    value: float | None
    reason: str


@dataclass
class Ctx:
    bars: list[Bar]
    index_bars: list[Bar] | None = None
    peer_momentum: dict[str, float] | None = None
    isin: str = ""
    cfg: dict[str, Any] = field(default_factory=dict)


# ChartSpec: {"dates": [str], "panels": [Panel], "markers": [{"day","label"}], "caption": str}
# Panel: {"title","kind": price|line|bars, "series": [{"name","values","style"}], "y_bands": [{"lo","hi","label"}]}
ChartSpec = dict


def linear_score(value: float, worst: float, best: float) -> float:
    """0 at `worst`, 100 at `best`, clamped; works when worst > best (lower is better)."""
    if best == worst:
        return 50.0
    s = (value - worst) / (best - worst) * 100.0
    return max(0.0, min(100.0, s))
