"""Builder and chart helpers shared by the algorithm families."""
from __future__ import annotations

import math
from typing import Callable

from ier.algos.core import AlgoResult, ChartSpec, Ctx, linear_score
from ier.algos.registry import Algorithm, register

DEFAULT_POINTS = 252


def S(name: str, values: list, style: str = "line") -> dict:
    return {"name": name, "values": values, "style": style}


def P(title: str, kind: str, series: list[dict], y_bands: list[dict] | None = None) -> dict:
    return {"title": title, "kind": kind, "series": series, "y_bands": y_bands or []}


def band_for(ctx: Ctx, algo_id: str, default: tuple[float, float]) -> tuple[float, float]:
    b = (ctx.cfg.get("algos") or {}).get(algo_id) or {}
    return float(b.get("worst", default[0])), float(b.get("best", default[1]))


def need(ctx: Ctx, n: int) -> AlgoResult | None:
    return AlgoResult(None, None, f"needs {n} daily prices") if len(ctx.bars) < n else None


def empty_chart(n: int) -> ChartSpec:
    return {"dates": [], "panels": [], "markers": [], "caption": f"needs {n} daily prices", "as_of": None}


def make_algo(*, id: str, name: str, family: str, horizon: str, min_history: int, description: str,
              value_fn: Callable[[list[float], Ctx], tuple[float, str] | None], band: tuple[float, float],
              chart_fn: Callable[[Ctx, list[float]], tuple[list[dict], list[dict], str]] | None = None,
              score_fn: Callable[[float, tuple[float, float], list[float]], float] | None = None, unit: str = "%") -> Algorithm:
    def compute(ctx: Ctx) -> AlgoResult:
        short = need(ctx, min_history)
        if short:
            return short
        closes = [b.close for b in ctx.bars]
        got = value_fn(closes, ctx)
        if isinstance(got, AlgoResult):
            return got
        if got is None:
            return AlgoResult(None, None, "no price variation")
        value, note = got
        if not math.isfinite(value):
            return AlgoResult(None, None, "calculation not finite")
        bd = band_for(ctx, id, band)
        score = score_fn(value, bd, closes) if score_fn else linear_score(value, *bd)
        return AlgoResult(round(score, 2), round(value, 4), f"{name}: {value:.2f}{unit}" + (f" ({note})" if note else ""))

    def chart(ctx: Ctx) -> ChartSpec:
        if len(ctx.bars) < min_history or chart_fn is None:
            return empty_chart(min_history)
        closes = [b.close for b in ctx.bars]
        panels, markers, caption = chart_fn(ctx, closes)
        k = min(int(ctx.cfg.get("points", DEFAULT_POINTS)), len(closes))
        dates = [b.day.isoformat() for b in ctx.bars][-k:]
        keep = set(dates)
        for p in panels:
            for s in p["series"]:
                s["values"] = [None if v is None or (isinstance(v, float) and not math.isfinite(v)) else v
                               for v in s["values"][-k:]]
        return {"dates": dates, "panels": panels, "markers": [m for m in markers if m["day"] in keep],
                "caption": caption, "as_of": ctx.bars[-1].day.isoformat()}

    return register(Algorithm(id, name, family, horizon, min_history, description, compute,
                              chart if chart_fn else None, band))


def days(ctx: Ctx) -> list[str]:
    return [b.day.isoformat() for b in ctx.bars]
