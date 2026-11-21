"""Algorithms inside the company run: stored scores, the capped blend, and failure isolation."""
import csv
from datetime import timedelta

import pytest
from sqlalchemy import select

from ier.algos import config as algo_config
from ier.algos.blend import blend_scores
from ier.db.models import AlgoScore, Scorecard
from ier.graph import company as cg
from tests.graph.test_company import _company, stubs, world  # noqa: F401


def _prices(path, n=300):
    end = cg._today()
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Date", "Close", "Volume"])
        for i in range(n):
            d = end - timedelta(days=n - 1 - i)
            w.writerow([d.isoformat(), 100 + i * 0.2 + (i % 9), 5000 + (i % 7) * 100])


def _run(world, tmp_path, run_id="r1", **kw):
    s, a, _ = world
    d = tmp_path / a.isin
    d.mkdir(exist_ok=True)
    _prices(d / "prices__daily__2026-11-01.csv")
    comp = _company(s, a)
    res = cg.run_company_agent(s, comp.id, run_id, filings_config={"local_dir": str(tmp_path)}, **kw)
    return s, a, res


def _card(s, a):
    return s.scalars(select(Scorecard).where(Scorecard.instrument_id == a.id)).one()


def test_scores_are_stored_and_weight_zero_leaves_scores_unchanged(world, stubs, tmp_path):
    s, a, res = _run(world, tmp_path)
    assert res.status == "completed"
    rows = s.scalars(select(AlgoScore).where(AlgoScore.instrument_id == a.id)).all()
    assert sum(1 for r in rows if r.score is not None) >= 20 and all(r.source_id for r in rows)
    card = _card(s, a)
    assert card.algo_weight == 0.0
    assert card.long_term_score == card.rules_long_score and card.short_term_score == card.rules_short_score
    assert card.algo_long_score is not None


def test_nonzero_weight_blends_and_records_it(world, stubs, tmp_path, monkeypatch):
    real = algo_config.load_algo_config()
    monkeypatch.setattr(cg, "load_algo_config", lambda: {**real, "blend_weight": 0.2})
    s, a, res = _run(world, tmp_path)
    card = _card(s, a)
    assert card.algo_weight == 0.2
    assert card.long_term_score == pytest.approx(blend_scores(card.rules_long_score, card.algo_long_score, 0.2))


def test_algorithm_exception_does_not_fail_the_run(world, stubs, tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("algo crash")
    monkeypatch.setattr(cg, "run_algorithms", boom)
    s, a, res = _run(world, tmp_path)
    assert res.status == "completed"
    card = _card(s, a)
    assert card.long_term_score == card.rules_long_score and card.algo_long_score is None


def test_company_without_prices_has_no_algorithm_score(world, stubs):
    s, a, _ = world
    comp = _company(s, a)
    assert cg.run_company_agent(s, comp.id, "r1").status == "completed"
    assert _card(s, a).algo_long_score is None


def test_fingerprint_changes_with_algos_config(world, tmp_path, monkeypatch):
    s, a, _ = world
    one = cg.fingerprint(s, a.id, [], None)
    alt = tmp_path / "algos.yaml"
    alt.write_text("blend_weight: 0.1\n")
    monkeypatch.setattr(algo_config, "DEFAULT_PATH", alt)
    assert cg.fingerprint(s, a.id, [], None) != one
