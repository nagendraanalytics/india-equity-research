import pytest

from ier.algos import breakout, risk, volume  # noqa: F401
from ier.algos.registry import all_algorithms, get
from tests.algos.conftest import make_ctx

IDS = ["volatility_60d", "max_drawdown_252d", "downside_deviation_126d", "sharpe_like_126d", "bollinger_position_20",
       "zscore_50", "high52_proximity", "low52_distance", "donchian20_close_breakout", "obv_trend_50", "volume_ratio_20d"]


def _wave(n=320):
    return [100 + (i % 17) + i * 0.2 for i in range(n)], [1000 + (i % 5) * 50 for i in range(n)]


def test_eleven_registered():
    got = {a.id for a in all_algorithms() if a.family in {"risk", "breakout", "volume"}}
    assert got == set(IDS)


def test_max_drawdown_known():
    r = get("max_drawdown_252d").compute(make_ctx([100, 120, 90] + [130] * 249))
    assert r.value == pytest.approx(-25.0)


def test_volatility_zero_for_flat_series_scores_best():
    r = get("volatility_60d").compute(make_ctx([100.0] * 80))
    assert r.value == 0 and r.score == 100


def test_high52_at_new_high_is_100():
    r = get("high52_proximity").compute(make_ctx([100 + i for i in range(260)]))
    assert r.value == pytest.approx(100.0) and r.score == 100


def test_donchian_breakout_marks_the_day():
    closes = [100.0] * 40 + [105.0]
    ctx = make_ctx(closes)
    r = get("donchian20_close_breakout").compute(ctx)
    assert r.score == 100
    spec = get("donchian20_close_breakout").chart(ctx)
    assert any(m["day"] == ctx.bars[-1].day.isoformat() for m in spec["markers"])


def test_obv_without_volume_is_none_with_reason():
    r = get("obv_trend_50").compute(make_ctx([100 + i for i in range(80)]))
    assert r.score is None and r.reason == "needs volume data"


def test_bollinger_flat_series_no_division_by_zero():
    r = get("bollinger_position_20").compute(make_ctx([100.0] * 30))
    assert r.score is None and r.reason == "no price variation"


def test_sharpe_flat_series_none():
    assert get("sharpe_like_126d").compute(make_ctx([100.0] * 140)).score is None


def test_volume_ratio_leans_with_price_move():
    up = get("volume_ratio_20d").compute(make_ctx([100.0] * 21 + [102.0], [100] * 21 + [300]))
    dn = get("volume_ratio_20d").compute(make_ctx([100.0] * 21 + [98.0], [100] * 21 + [300]))
    assert up.score > 50 > dn.score


def test_short_history_none_everywhere():
    for i in IDS:
        r = get(i).compute(make_ctx([100.0] * 5, [10] * 5))
        assert r.score is None and r.reason.startswith("needs")


def test_flat_series_never_raises_or_out_of_range():
    for i in IDS:
        r = get(i).compute(make_ctx([100.0] * 300, [10] * 300))
        assert r.score is None or 0 <= r.score <= 100


@pytest.mark.parametrize("algo_id", IDS)
def test_chart_lengths(algo_id):
    closes, vols = _wave()
    spec = get(algo_id).chart(make_ctx(closes, vols))
    assert spec["caption"] and spec["panels"]
    for p in spec["panels"]:
        for s in p["series"]:
            assert len(s["values"]) == len(spec["dates"])
