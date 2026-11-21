"""Volume algorithms. Need volume data; otherwise they report what is missing. Bands are PLACEHOLDERs."""
from __future__ import annotations

from ier.algos._common import P, S, make_algo
from ier.algos.core import AlgoResult
from ier.algos.maths import obv_series, sma_series


def _have_volume(ctx, n):
    w = ctx.bars[-n:]
    return sum(1 for b in w if b.volume) >= 0.8 * len(w)


def _obv_value(c, ctx):
    if not _have_volume(ctx, 51):
        return AlgoResult(None, None, "needs volume data")
    obv = obv_series(ctx.bars[-51:])
    total = sum(b.volume or 0 for b in ctx.bars[-50:])
    return ((obv[-1] - obv[0]) / total * 100, "") if total else None


def _obv_chart(ctx, c):
    vol = [b.volume for b in ctx.bars]
    return ([P("Price", "price", [S("Close", c)]), P("On-balance volume", "line", [S("OBV", obv_series(ctx.bars))]),
             P("Volume", "bars", [S("Volume", vol, "bar")])],
            [], "On-balance volume adds volume on up days and subtracts it on down days.")


def _ratio(ctx):
    v = [b.volume for b in ctx.bars]
    prev = v[-21:-1]
    if not v[-1] or sum(1 for x in prev if x) < 16:
        return None
    return v[-1] / (sum(x or 0 for x in prev) / 20)


def _ratio_value(c, ctx):
    r = _ratio(ctx)
    if r is None:
        return AlgoResult(None, None, "needs volume data")
    return (r, "up day" if c[-1] > c[-2] else "down day" if c[-1] < c[-2] else "unchanged")


def _ratio_score(value, bd, c):
    d = 1 if c[-1] > c[-2] else -1 if c[-1] < c[-2] else 0
    return 50 + d * max(-25.0, min(25.0, (value - 1) * 25))


def _ratio_chart(ctx, c):
    v = [b.volume for b in ctx.bars]
    return ([P("Volume and 20-day average", "bars", [S("Volume", v, "bar"), S("20-day average", sma_series([x or 0.0 for x in v], 20), "dashed")])],
            [], "Daily volume against its 20-day average.")


def _install():
    obv = make_algo(id="obv_trend_50", name="On-balance volume trend, 50 days", family="volume", horizon="short", min_history=51,
                    description="Change in on-balance volume over 50 days relative to total volume, in percent. Needs volume on most days.",
                    value_fn=_obv_value, band=(-30, 30), chart_fn=_obv_chart)
    ratio = make_algo(id="volume_ratio_20d", name="Volume vs 20-day average", family="volume", horizon="short", min_history=21,
                      description="Last day's volume divided by the average of the previous 20 days. Leans with the direction of the day's price move.",
                      value_fn=_ratio_value, band=(0, 3), chart_fn=_ratio_chart, score_fn=_ratio_score, unit="x")
    return obv, ratio


_install()
