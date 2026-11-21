"""Volatility and risk algorithms. All bands are PLACEHOLDERs; lower risk scores higher."""
from __future__ import annotations

import math

from ier.algos._common import P, S, make_algo
from ier.algos.core import linear_score
from ier.algos.maths import drawdown_series, pct_returns, rolling_stdev, sma_series, stdev

ANN = math.sqrt(252)


def _bounded(value, bd, c):
    """Mean-reversion reading bounded to 30-70: far above the band leans negative, below leans positive."""
    return 30 + 0.4 * linear_score(value, bd[0], bd[1])


def _roll(c, n, fn):
    out = [None] * len(c)
    for i in range(n, len(c)):
        out[i] = fn(c[i - n:i + 1])
    return out


def _vol(w):
    r = pct_returns(w)
    s = stdev(r)
    return None if s is None else s * ANN * 100


def _downside(w):
    r = pct_returns(w)
    return math.sqrt(sum(min(x, 0) ** 2 for x in r) / len(r)) * ANN * 100 if r else None


def _sharpe(w):
    r = pct_returns(w)
    s = stdev(r)
    return None if not s else sum(r) / len(r) / s * ANN


def _quartiles(vals):
    xs = sorted(v for v in vals if v is not None)
    return (xs[len(xs) // 4], xs[(3 * len(xs)) // 4]) if xs else (0, 0)


def _vol_chart(ctx, c):
    v = _roll(c, 60, _vol)
    lo, hi = _quartiles(v)
    return ([P("Volatility (60-day, annualised %)", "line", [S("Volatility", v)], [{"lo": lo, "hi": hi, "label": "usual range (25th-75th percentile)"}])],
            [], "Rolling 60-day volatility of daily returns; shaded band is its usual range.")


make_algo(id="volatility_60d", name="Volatility, 60 days", family="risk", horizon="long", min_history=61,
          description="Annualised standard deviation of the last 60 daily returns, in percent. Lower is better.",
          value_fn=lambda c, ctx: (_vol(c[-61:]), ""), band=(60, 15), chart_fn=_vol_chart)


def _dd_chart(ctx, c):
    return ([P("Drawdown from peak (%)", "line", [S("Drawdown", [d * 100 for d in drawdown_series(c)])])],
            [], "How far the close sits below its running peak; zero means at a new high.")


make_algo(id="max_drawdown_252d", name="Worst drawdown, 1 year", family="risk", horizon="long", min_history=252,
          description="Largest fall from a peak to a later low within the last 252 closes, in percent (negative).",
          value_fn=lambda c, ctx: (min(drawdown_series(c[-252:])) * 100, ""), band=(-60, -5), chart_fn=_dd_chart)

make_algo(id="downside_deviation_126d", name="Downside deviation, 126 days", family="risk", horizon="long", min_history=127,
          description="Annualised root-mean-square of the negative daily returns over 126 days, in percent. Lower is better.",
          value_fn=lambda c, ctx: (_downside(c[-127:]), ""), band=(50, 5),
          chart_fn=lambda ctx, c: ([P("Downside deviation (126-day, annualised %)", "line", [S("Downside deviation", _roll(c, 126, _downside))])],
                                   [], "Rolling size of the down days only."))

make_algo(id="sharpe_like_126d", name="Return per unit of risk, 126 days", family="risk", horizon="long", min_history=127,
          description="Mean daily return divided by its standard deviation, annualised, over 126 days. No risk-free rate is subtracted.",
          value_fn=lambda c, ctx: (None if _sharpe(c[-127:]) is None else (_sharpe(c[-127:]), "")), band=(-1, 2),
          chart_fn=lambda ctx, c: ([P("Return per unit of risk (126-day)", "line", [S("Ratio", _roll(c, 126, _sharpe)), S("Zero", [0.0] * len(c), "dashed")])],
                                   [], "Rolling return-to-risk ratio; above zero means the window gained."))


def _boll(c):
    w = c[-20:]
    s = stdev(w)
    return None if not s else (c[-1] - sum(w) / 20) / (2 * s)


def _boll_value(c, ctx):
    p = _boll(c)
    if p is None:
        return None
    return p, "near upper band" if p >= 0.8 else "near lower band" if p <= -0.8 else "inside bands"


def _boll_chart(ctx, c):
    m = sma_series(c, 20)
    sd = rolling_stdev(c, 20)
    up = [None if a is None else a + 2 * b for a, b in zip(m, sd)]
    dn = [None if a is None else a - 2 * b for a, b in zip(m, sd)]
    return ([P("Price and Bollinger bands", "price", [S("Close", c), S("20-day average", m, "dashed"), S("Upper band", up, "band_hi"), S("Lower band", dn, "band_lo")])],
            [], "Close with bands two standard deviations around the 20-day average.")


make_algo(id="bollinger_position_20", name="Position in Bollinger bands", family="risk", horizon="short", min_history=20,
          description="Where the close sits between the 20-day Bollinger bands: 0 at the average, +1 at the upper band, -1 at the lower. Read as mean-reverting, bounded to a mild lean.",
          value_fn=_boll_value, band=(1, -1), chart_fn=_boll_chart, score_fn=_bounded, unit="")


def _z(c):
    w = c[-50:]
    s = stdev(w)
    return None if not s else (c[-1] - sum(w) / 50) / s


def _z_chart(ctx, c):
    m = sma_series(c, 50)
    sd = rolling_stdev(c, 50)
    z = [None if a is None or not b else (x - a) / b for x, a, b in zip(c, m, sd)]
    return ([P("Distance from 50-day average (z-score)", "line", [S("z-score", z)], [{"lo": -2, "hi": 2, "label": "within two standard deviations"}])],
            [], "Number of standard deviations the close is from its 50-day average.")


make_algo(id="zscore_50", name="Z-score vs 50-day average", family="risk", horizon="short", min_history=50,
          description="How many standard deviations the last close is from its 50-day average. Read as mean-reverting, bounded to a mild lean.",
          value_fn=lambda c, ctx: (None if _z(c) is None else (_z(c), "")), band=(2, -2), chart_fn=_z_chart, score_fn=_bounded, unit="")
