"""Trend algorithms: moving averages, MACD and a fitted trend slope. All bands are PLACEHOLDERs."""
from __future__ import annotations

import math

from ier.algos._common import P, S, days, make_algo
from ier.algos.core import linear_score
from ier.algos.maths import ema_series, linreg, sma, sma_series


def _sma_cross(ctx, closes):
    a, b = sma_series(closes, 50), sma_series(closes, 200)
    d = days(ctx)
    marks = []
    for i in range(1, len(closes)):
        if None in (a[i], b[i], a[i - 1], b[i - 1]):
            continue
        if (a[i - 1] - b[i - 1]) * (a[i] - b[i]) < 0:
            marks.append({"day": d[i], "label": "50-day crossed above 200-day" if a[i] > b[i] else "50-day crossed below 200-day"})
    return ([P("Price and moving averages", "price", [S("Close", closes), S("50-day average", a, "dashed"), S("200-day average", b, "dashed")])],
            marks, "Close with the 50-day and 200-day averages; markers show where they crossed.")


make_algo(id="sma50_vs_sma200", name="50-day vs 200-day average", family="trend", horizon="long", min_history=200,
          description="Percent gap between the 50-day and the 200-day simple average of closes. Positive when the shorter average is above the longer one.",
          value_fn=lambda c, ctx: ((sma(c, 50) / sma(c, 200) - 1) * 100, ""), band=(-10, 10), chart_fn=_sma_cross)

make_algo(id="price_vs_sma200", name="Price vs 200-day average", family="trend", horizon="long", min_history=200,
          description="Percent gap between the last close and the 200-day simple average.",
          value_fn=lambda c, ctx: ((c[-1] / sma(c, 200) - 1) * 100, ""), band=(-20, 20),
          chart_fn=lambda ctx, c: ([P("Price vs 200-day average", "price", [S("Close", c), S("200-day average", sma_series(c, 200), "dashed")])],
                                   [], "Close against its 200-day average."))


def _ema_vals(c):
    return ema_series(c, 12), ema_series(c, 26)


make_algo(id="ema12_vs_ema26", name="12-day vs 26-day exponential average", family="trend", horizon="short", min_history=35,
          description="Percent gap between the 12-day and 26-day exponential averages of closes.",
          value_fn=lambda c, ctx: ((_ema_vals(c)[0][-1] / _ema_vals(c)[1][-1] - 1) * 100, ""), band=(-5, 5),
          chart_fn=lambda ctx, c: ([P("Price and exponential averages", "price", [S("Close", c), S("12-day", _ema_vals(c)[0], "dashed"), S("26-day", _ema_vals(c)[1], "dashed")])],
                                   [], "Close with the 12-day and 26-day exponential averages."))


def _macd(c):
    e12, e26 = _ema_vals(c)
    line = [a - b for a, b in zip(e12, e26)]
    sig = ema_series(line, 9)
    return line, sig, [a - b for a, b in zip(line, sig)]


def _macd_chart(ctx, c):
    line, sig, hist = _macd(c)
    return ([P("Price", "price", [S("Close", c)]),
             P("MACD", "line", [S("MACD line", line), S("Signal line", sig, "dashed"), S("Histogram", hist, "bar")])],
            [], "MACD line, its 9-day signal line and the histogram (their gap).")


make_algo(id="macd_histogram", name="MACD histogram", family="trend", horizon="short", min_history=35,
          description="MACD (12-day minus 26-day exponential average) minus its 9-day average, as a percent of the last close.",
          value_fn=lambda c, ctx: (_macd(c)[2][-1] / c[-1] * 100, ""), band=(-2, 2), chart_fn=_macd_chart)


def _slope(c):
    fit = linreg([math.log(x) for x in c[-126:]])
    return fit


def _slope_value(c, ctx):
    fit = _slope(c)
    if fit is None:
        return None
    return fit[0] * 252 * 100, f"fit quality R² {fit[1]:.2f}"


def _slope_score(value, bd, c):
    shrink = 1 - (1 - _slope(c)[1]) * 0.5
    return 50 + (linear_score(value, *bd) - 50) * shrink


def _slope_chart(ctx, c):
    logs = [math.log(x) for x in c[-126:]]
    fit = linreg(logs)
    n = len(c)
    mid = fit[0] * (len(logs) - 1) / 2
    ybar = sum(logs) / len(logs)
    line, lo, hi = [None] * (n - 126), [None] * (n - 126), [None] * (n - 126)
    for i in range(len(logs)):
        f = ybar + fit[0] * (i - (len(logs) - 1) / 2)
        line.append(math.exp(f)); lo.append(math.exp(f - fit[2])); hi.append(math.exp(f + fit[2]))
    return ([P("Price and fitted trend", "price", [S("Close", c), S("Fitted trend (126 days)", line, "dashed"), S("Trend band low", lo, "band_lo"), S("Trend band high", hi, "band_hi")])],
            [], f"Straight-line fit to the last 126 closes (log scale); R² {fit[1]:.2f}.")


make_algo(id="trend_slope_126d", name="Trend slope, 126 days", family="trend", horizon="long", min_history=126,
          description="Annualised slope of a straight-line fit to the last 126 log closes, in percent. The score is pulled toward neutral when the fit is poor (low R²).",
          value_fn=_slope_value, band=(-30, 30), chart_fn=_slope_chart, score_fn=_slope_score)
