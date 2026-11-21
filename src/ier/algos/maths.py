"""Pure maths helpers. Short or degenerate input yields None / empty, never an exception."""
from __future__ import annotations

import math

from ier.algos.core import Bar


def sma(xs: list[float], n: int) -> float | None:
    return sum(xs[-n:]) / n if n > 0 and len(xs) >= n else None


def sma_series(xs: list[float], n: int) -> list[float | None]:
    out: list[float | None] = []
    run = 0.0
    for i, x in enumerate(xs):
        run += x
        if i >= n:
            run -= xs[i - n]
        out.append(run / n if i >= n - 1 else None)
    return out


def ema_series(xs: list[float], n: int) -> list[float]:
    if not xs:
        return []
    a = 2.0 / (n + 1)
    out = [float(xs[0])]
    for x in xs[1:]:
        out.append(a * x + (1 - a) * out[-1])
    return out


def stdev(xs: list[float]) -> float | None:
    if len(xs) < 2:
        return None
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def pct_returns(xs: list[float]) -> list[float]:
    return [b / a - 1 for a, b in zip(xs, xs[1:]) if a > 0]


def rsi(xs: list[float], n: int = 14) -> float | None:
    if len(xs) < n + 1:
        return None
    ch = [b - a for a, b in zip(xs, xs[1:])]
    gain = sum(max(x, 0) for x in ch[:n]) / n
    loss = sum(max(-x, 0) for x in ch[:n]) / n
    for x in ch[n:]:
        gain = (gain * (n - 1) + max(x, 0)) / n
        loss = (loss * (n - 1) + max(-x, 0)) / n
    return 100.0 if loss == 0 else 100 - 100 / (1 + gain / loss)


def linreg(xs: list[float]) -> tuple[float, float, float] | None:
    """(slope, r2, stderr of the fit) over index 0..n-1."""
    n = len(xs)
    if n < 3:
        return None
    mx = (n - 1) / 2
    my = sum(xs) / n
    sxx = sum((i - mx) ** 2 for i in range(n))
    sxy = sum((i - mx) * (y - my) for i, y in enumerate(xs))
    slope = sxy / sxx
    syy = sum((y - my) ** 2 for y in xs)
    ss_res = sum((y - (my + slope * (i - mx))) ** 2 for i, y in enumerate(xs))
    r2 = 1.0 if syy == 0 else max(0.0, 1 - ss_res / syy)
    return slope, r2, math.sqrt(ss_res / (n - 2))


def drawdown_series(xs: list[float]) -> list[float]:
    peak = -math.inf
    out = []
    for x in xs:
        peak = max(peak, x)
        out.append(x / peak - 1 if peak > 0 else 0.0)
    return out


def obv_series(bars: list[Bar]) -> list[float]:
    out = [0.0] if bars else []
    for a, b in zip(bars, bars[1:]):
        v = b.volume or 0.0
        out.append(out[-1] + (v if b.close > a.close else -v if b.close < a.close else 0.0))
    return out


def rolling_stdev(xs: list[float], n: int) -> list[float | None]:
    return [stdev(xs[i - n + 1:i + 1]) if i >= n - 1 else None for i in range(len(xs))]


def rsi_series(xs: list[float], n: int = 14) -> list[float | None]:
    out: list[float | None] = [None] * len(xs)
    if len(xs) < n + 1:
        return out
    ch = [b - a for a, b in zip(xs, xs[1:])]
    gain = sum(max(x, 0) for x in ch[:n]) / n
    loss = sum(max(-x, 0) for x in ch[:n]) / n

    def val(g, l):
        return 100.0 if l == 0 else 100 - 100 / (1 + g / l)

    out[n] = val(gain, loss)
    for i, x in enumerate(ch[n:], start=n + 1):
        gain = (gain * (n - 1) + max(x, 0)) / n
        loss = (loss * (n - 1) + max(-x, 0)) / n
        out[i] = val(gain, loss)
    return out
