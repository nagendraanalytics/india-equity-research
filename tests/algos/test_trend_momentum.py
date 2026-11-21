import pytest

from ier.algos import momentum, trend  # noqa: F401  (registers)
from ier.algos.core import AlgoResult
from ier.algos.registry import all_algorithms, get
from tests.algos.conftest import make_ctx

FAMILIES = {"trend", "momentum"}


def test_eleven_algorithms_registered():
    ids = {a.id for a in all_algorithms() if a.family in FAMILIES}
    assert ids == {"sma50_vs_sma200", "price_vs_sma200", "ema12_vs_ema26", "macd_histogram", "trend_slope_126d",
                   "mom_1m", "mom_3m", "mom_6m", "mom_12m_skip_1m", "rsi14", "roc_20"}


def test_sma_cross_rising_series():
    r = get("sma50_vs_sma200").compute(make_ctx([100 + i for i in range(260)]))
    assert r.value > 0 and r.score > 50


def test_short_history_is_none():
    r = get("price_vs_sma200").compute(make_ctx([100 + i for i in range(150)]))
    assert r == AlgoResult(None, None, "needs 200 daily prices")


def test_mom_3m_exact():
    closes = [100.0] * 100 + [110.0]
    r = get("mom_3m").compute(make_ctx(closes))
    assert r.value == pytest.approx(10.0)


def test_rsi_all_up_is_overbought_reason():
    r = get("rsi14").compute(make_ctx([100 + i for i in range(40)]))
    assert r.value == 100.0 and "overbought" in r.reason and r.score < 50


def test_rsi_oversold_leans_positive():
    r = get("rsi14").compute(make_ctx([200 - i for i in range(40)]))
    assert "oversold" in r.reason and r.score > 50


def test_flat_series_scores_neutral_not_error():
    for a in all_algorithms():
        if a.family in FAMILIES:
            r = a.compute(make_ctx([100.0] * 300))
            assert r.score is None or 0 <= r.score <= 100


def test_config_band_overrides_default():
    base = get("mom_3m").compute(make_ctx([100.0] * 100 + [110.0]))
    cfg = {"algos": {"mom_3m": {"worst": 0, "best": 10}}}
    over = get("mom_3m").compute(make_ctx([100.0] * 100 + [110.0], cfg=cfg))
    assert over.score == 100 and base.score != 100


@pytest.mark.parametrize("algo_id", sorted(a for a in ["sma50_vs_sma200", "price_vs_sma200", "ema12_vs_ema26",
                                                        "macd_histogram", "trend_slope_126d", "mom_1m", "mom_3m",
                                                        "mom_6m", "mom_12m_skip_1m", "rsi14", "roc_20"]))
def test_chart_lengths(algo_id):
    closes = [100 + (i % 17) + i * 0.2 for i in range(320)]
    spec = get(algo_id).chart(make_ctx(closes))
    assert spec["caption"] and spec["panels"]
    for p in spec["panels"]:
        for s in p["series"]:
            assert len(s["values"]) == len(spec["dates"])


def test_chart_short_history_has_no_panels():
    spec = get("price_vs_sma200").chart(make_ctx([100.0] * 20))
    assert spec["panels"] == [] and "needs 200" in spec["caption"]
