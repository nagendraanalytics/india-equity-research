import pytest

from ier.algos import fundamental, relative  # noqa: F401
from ier.algos.core import Ctx
from ier.algos.registry import all_algorithms, get
from tests.algos.conftest import make_bars, make_ctx

PHASE2 = ["pe_ratio", "earnings_yield", "peg", "piotroski_f", "altman_z", "magic_formula", "dupont_roe", "graham_number"]


def _ctx(company, index=None, peers=None):
    return Ctx(bars=make_bars(company), index_bars=None if index is None else make_bars(index), peer_momentum=peers,
               isin="INE009A01021", cfg={})


def test_rs_outperformance_positive():
    comp = [100.0] * 64
    comp[-1] = 120.0
    idx = [100.0] * 64
    idx[-1] = 110.0
    r = get("rs_vs_sector_index_3m").compute(_ctx(comp, idx))
    assert r.value == pytest.approx(10.0) and r.score > 50


def test_missing_index_reason():
    r = get("rs_vs_sector_index_3m").compute(_ctx([100.0] * 70))
    assert r.score is None and r.reason == "no index data for this sector"


def test_index_dates_do_not_line_up():
    from datetime import date
    from ier.algos.core import Bar
    comp = make_bars([100.0] * 70)
    idx = [Bar(date(2030, 1, 1 + i), 100.0) for i in range(20)]
    r = get("rs_vs_sector_index_3m").compute(Ctx(bars=comp, index_bars=idx, isin="x", cfg={}))
    assert r.score is None and r.reason == "index dates do not line up"


def test_rank_percentile():
    closes = [100.0] * 126 + [110.0]
    peers = {"A": 1.0, "B": 2.0, "C": 3.0, "INE009A01021": 4.0, "D": 5.0}
    r = get("rs_rank_in_sector").compute(_ctx(closes, peers=peers))
    assert r.value == 75.0 and r.score == 75.0


def test_fewer_than_three_peers():
    r = get("rs_rank_in_sector").compute(_ctx([100.0] * 130, peers={"A": 1.0, "INE009A01021": 2.0}))
    assert r.score is None and r.reason == "needs 3 peers in the sector"


def test_rs_charts_line_up():
    comp = [100 + i * 0.3 for i in range(300)]
    idx = [100 + i * 0.1 for i in range(300)]
    for i in ("rs_vs_sector_index_3m", "rs_vs_sector_index_12m"):
        spec = get(i).chart(_ctx(comp, idx))
        assert spec["panels"]
        for p in spec["panels"]:
            for s in p["series"]:
                assert len(s["values"]) == len(spec["dates"])


def test_rank_chart_highlights_company():
    peers = {"A": 1.0, "B": 2.0, "C": 3.0, "INE009A01021": 4.0}
    spec = get("rs_rank_in_sector").chart(_ctx([100.0] * 130, peers=peers))
    assert spec["panels"][0]["kind"] == "bars" and any(m["day"] == "INE009A01021" for m in spec["markers"])


def test_phase2_algorithms_registered_and_none():
    got = {a.id: a for a in all_algorithms() if a.family == "value_quality"}
    assert set(got) == set(PHASE2)
    for a in got.values():
        r = a.compute(make_ctx([100.0] * 300))
        assert r.score is None and r.reason.startswith("needs") and a.chart is None
