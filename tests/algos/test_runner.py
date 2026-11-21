from datetime import date, timedelta

import pytest

from ier.algos import registry
from ier.algos.composite import Composite, compute_composite
from ier.algos.core import AlgoResult
from ier.algos.registry import Algorithm, all_algorithms
from ier.algos.runner import load_all, run_algorithms
from tests.algos.conftest import make_bars

CFG = {"max_stale_days": 10, "enabled_families": None, "algos": {}}


def _bars(n=300, end=date(2026, 11, 20)):
    return make_bars([100 + i * 0.1 + (i % 7) for i in range(n)], [1000] * n, start=end - timedelta(days=n - 1))


def test_runner_covers_all_algorithms():
    load_all()
    res = run_algorithms(_bars(), "INE009A01021", CFG, today=date(2026, 11, 21))
    assert set(res) == {a.id for a in all_algorithms()}
    assert sum(1 for r in res.values() if r.score is not None) >= 20


def test_staleness_blanks_everything():
    res = run_algorithms(_bars(end=date(2026, 10, 1)), "x", CFG, today=date(2026, 11, 21))
    assert all(r.score is None and r.reason == "prices stale (last 2026-10-01)" for r in res.values())


def test_no_prices_at_all():
    res = run_algorithms([], "x", CFG, today=date(2026, 11, 21))
    assert all(r.score is None and r.reason == "needs daily prices" for r in res.values())


def test_one_failing_algorithm_does_not_stop_others(monkeypatch):
    load_all()
    a = registry.get("mom_3m")

    def boom(ctx):
        raise RuntimeError("x")

    monkeypatch.setitem(registry._REGISTRY, "mom_3m", Algorithm(a.id, a.name, a.family, a.horizon, a.min_history, a.description, boom, a.chart))
    res = run_algorithms(_bars(), "x", CFG, today=date(2026, 11, 21))
    assert res["mom_3m"].score is None and res["mom_3m"].reason == "calculation failed"
    assert res["mom_1m"].score is not None


def test_disabled_family_skipped():
    res = run_algorithms(_bars(), "x", {**CFG, "enabled_families": ["trend"]}, today=date(2026, 11, 21))
    assert {registry.get(k).family for k in res} == {"trend"}


def _a(id, hz, fam="trend"):
    return Algorithm(id, id, fam, hz, 1, "", lambda c: AlgoResult(None, None, ""), None)


def test_composite_equal_weights():
    algos = [_a("a", "long"), _a("b", "long"), _a("c", "short")]
    c = compute_composite({"a": AlgoResult(40, 1, ""), "b": AlgoResult(80, 1, ""), "c": AlgoResult(None, None, "")}, algos)
    assert c.long == 60 and c.short is None and c.n_available == 2 and c.n_total == 3
    assert c.confidence == pytest.approx(2 / 3)


def test_composite_all_none():
    c = compute_composite({"a": AlgoResult(None, None, "")}, [_a("a", "long")])
    assert c.long is None and c.confidence == 0.0 and c.agreement is None


def test_agreement():
    algos = [_a(str(i), "long") for i in range(4)]
    res = {str(i): AlgoResult(s, 0, "") for i, s in enumerate([60, 70, 80, 30])}
    assert compute_composite(res, algos).agreement == 0.75


def test_composite_ignores_pending_phase2():
    algos = [_a("a", "long"), _a("pe", "long", fam="value_quality")]
    c = compute_composite({"a": AlgoResult(50, 0, ""), "pe": AlgoResult(None, None, "needs x")}, algos)
    assert c.n_total == 1 and c.confidence == 1.0
