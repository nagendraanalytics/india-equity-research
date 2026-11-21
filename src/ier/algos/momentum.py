"""Momentum algorithms: returns over a window and RSI. All bands are PLACEHOLDERs."""
from __future__ import annotations

from ier.algos._common import P, S, days, make_algo
from ier.algos.core import linear_score
from ier.algos.maths import rsi, rsi_series


def _ret_algo(id, name, horizon, lookback, band, desc, skip=0):
    n = lookback + skip + 1

    def value(c, ctx):
        end = c[-1 - skip]
        start = c[-1 - skip - lookback]
        return (end / start - 1) * 100, ""

    def chart(ctx, c):
        vals = [None] * len(c)
        for i in range(lookback + skip, len(c)):
            vals[i] = (c[i - skip] / c[i - skip - lookback] - 1) * 100
        return ([P("Price", "price", [S("Close", c)]),
                 P(f"{name} (rolling)", "line", [S(name, vals), S("Zero", [0.0] * len(c), "dashed")])],
                [], f"{name} measured on every day; above zero means the price rose over the window.")

    make_algo(id=id, name=name, family="momentum", horizon=horizon, min_history=n, description=desc,
              value_fn=value, band=band, chart_fn=chart)


_ret_algo("mom_1m", "1-month return", "short", 21, (-10, 10), "Percent change in close over 21 trading days.")
_ret_algo("mom_3m", "3-month return", "short", 63, (-20, 20), "Percent change in close over 63 trading days.")
_ret_algo("mom_6m", "6-month return", "long", 126, (-30, 30), "Percent change in close over 126 trading days.")
_ret_algo("mom_12m_skip_1m", "12-month return skipping last month", "long", 231, (-30, 50),
          "Percent change from 252 to 21 trading days ago; leaves out the latest month to avoid short-term reversal.", skip=21)
_ret_algo("roc_20", "Rate of change, 20 days", "short", 20, (-10, 10), "Percent change in close over 20 trading days.")


def _rsi_value(c, ctx):
    tail = c[-15:]
    if max(tail) == min(tail):
        return 50.0, "flat prices"
    v = rsi(c)
    return v, "overbought" if v >= 70 else "oversold" if v <= 30 else "neutral zone"


def _rsi_score(value, bd, c):
    # Mean-reversion reading: oversold leans positive, overbought leans negative, bounded to 30-70.
    return 30 + 0.4 * linear_score(value, bd[0], bd[1])


def _rsi_chart(ctx, c):
    return ([P("Price", "price", [S("Close", c)]),
             P("RSI (14)", "line", [S("RSI", rsi_series(c))], y_bands=[{"lo": 0, "hi": 30, "label": "oversold zone"}, {"lo": 70, "hi": 100, "label": "overbought zone"}])],
            [], "Relative strength index; above 70 is read as overbought, below 30 as oversold.")


make_algo(id="rsi14", name="RSI (14)", family="momentum", horizon="short", min_history=15,
          description="Relative strength index over 14 days. Read as mean-reverting: oversold leans positive, overbought leans negative.",
          value_fn=_rsi_value, band=(70, 30), chart_fn=_rsi_chart, score_fn=_rsi_score, unit="")
