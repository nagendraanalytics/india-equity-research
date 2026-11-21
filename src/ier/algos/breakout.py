"""Breakout algorithms: 52-week range and Donchian channel. All bands are PLACEHOLDERs."""
from __future__ import annotations

from ier.algos._common import P, S, days, make_algo
from ier.algos.core import linear_score


def _rolling(c, n, fn):
    return [fn(c[max(0, i - n + 1):i + 1]) if i >= n - 1 else None for i in range(len(c))]


make_algo(id="high52_proximity", name="Closeness to 52-week high", family="breakout", horizon="long", min_history=252,
          description="Last close as a percent of the highest close in the last 252 days. 100 means at a new high.",
          value_fn=lambda c, ctx: (c[-1] / max(c[-252:]) * 100, ""), band=(60, 100),
          chart_fn=lambda ctx, c: ([P("Price and 52-week high", "price", [S("Close", c), S("52-week high", _rolling(c, 252, max), "dashed")])],
                                   [], "Close against its rolling 52-week high."))

make_algo(id="low52_distance", name="Distance above 52-week low", family="breakout", horizon="long", min_history=252,
          description="Percent the last close is above the lowest close in the last 252 days.",
          value_fn=lambda c, ctx: ((c[-1] / min(c[-252:]) - 1) * 100, ""), band=(0, 60),
          chart_fn=lambda ctx, c: ([P("Price and 52-week low", "price", [S("Close", c), S("52-week low", _rolling(c, 252, min), "dashed")])],
                                   [], "Close against its rolling 52-week low."))


def _donchian_value(c, ctx):
    prev = c[-21:-1]
    hi, lo = max(prev), min(prev)
    if c[-1] > hi:
        return 100.0, "closed above the 20-day high"
    if c[-1] < lo:
        return 0.0, "closed below the 20-day low"
    return ((c[-1] - lo) / (hi - lo) * 100 if hi > lo else 50.0), "inside the 20-day channel"


def _donchian_chart(ctx, c):
    d = days(ctx)
    up, dn, marks = [None] * len(c), [None] * len(c), []
    for i in range(20, len(c)):
        prev = c[i - 20:i]
        up[i], dn[i] = max(prev), min(prev)
        if c[i] > up[i]:
            marks.append({"day": d[i], "label": "closed above 20-day high"})
        elif c[i] < dn[i]:
            marks.append({"day": d[i], "label": "closed below 20-day low"})
    return ([P("Price and 20-day channel", "price", [S("Close", c), S("20-day high", up, "dashed"), S("20-day low", dn, "dashed")])],
            marks, "Close with the previous 20-day high and low; markers show closes outside the channel.")


make_algo(id="donchian20_close_breakout", name="20-day channel breakout", family="breakout", horizon="short", min_history=21,
          description="100 when the close is above the previous 20 closes' high, 0 when below their low, otherwise its position inside the channel.",
          value_fn=_donchian_value, band=(0, 100), chart_fn=_donchian_chart, unit="")
