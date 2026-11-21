"""The one place algorithm scores can touch a final score. Weight is capped; algorithms never create a score."""
from __future__ import annotations

from ier.algos.config import MAX_BLEND_WEIGHT


def blend_scores(rules: float | None, algo: float | None, weight: float) -> float | None:
    if rules is None:
        return None
    w = max(0.0, min(float(weight), MAX_BLEND_WEIGHT))
    if algo is None or w == 0.0:
        return rules
    return (1 - w) * rules + w * algo
