import pytest

from ier.algos import maths
from ier.algos.core import Bar
from datetime import date


def test_sma():
    assert maths.sma([1, 2, 3, 4, 5], 5) == 3
    assert maths.sma([1, 2], 5) is None


def test_sma_series_pads_none():
    assert maths.sma_series([1, 2, 3, 4], 2) == [None, 1.5, 2.5, 3.5]


def test_ema_series():
    assert maths.ema_series([1, 2, 3], 3) == [1.0, 1.5, 2.25]


def test_stdev_sample():
    assert maths.stdev([2, 4, 4, 4, 5, 5, 7, 9]) == pytest.approx(2.13809, abs=1e-5)
    assert maths.stdev([1]) is None


def test_pct_returns_skips_nonpositive_prior():
    assert maths.pct_returns([100, 110, 0, 5]) == [pytest.approx(0.1), -1.0]


def test_rsi_all_rising_is_100():
    assert maths.rsi([float(i) for i in range(1, 17)]) == 100.0
    assert maths.rsi([1.0, 2.0]) is None


def test_linreg_perfect_line():
    slope, r2, _ = maths.linreg([1, 2, 3, 4])
    assert slope == pytest.approx(1.0) and r2 == pytest.approx(1.0)
    assert maths.linreg([1.0]) is None


def test_drawdown_series():
    assert maths.drawdown_series([100, 120, 90, 130]) == [0, 0, -0.25, 0]


def test_obv_series():
    bars = [Bar(date(2026, 1, i + 1), c, 5.0) for i, c in enumerate([10, 11, 10.5, 10.5])]
    assert maths.obv_series(bars) == [0, 5, 0, 0]


def test_rolling_stdev_short_input():
    assert maths.rolling_stdev([1.0, 2.0], 5) == [None, None]


def test_rsi_series_matches_rsi_at_end():
    xs = [100 + (i % 7) - i * 0.1 for i in range(60)]
    s = maths.rsi_series(xs)
    assert len(s) == len(xs) and s[13] is None
    assert s[-1] == pytest.approx(maths.rsi(xs))
