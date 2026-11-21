import os
from datetime import date

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from ier.algos.core import AlgoResult
from ier.algos.store import latest_composite, latest_results, peer_momentum, save_results
from ier.db.models import AlgoScore, Instrument
from tests.db.conftest import ROOT, TEST_URL, assert_safe_test_db


def _inst(session, isin="INE009A01021", macro="Information Technology"):
    i = Instrument(isin=isin, name=isin, nse_code=isin[-4:], macro_sector=macro)
    session.add(i)
    session.flush()
    return i


def test_save_is_idempotent_per_day(session):
    i = _inst(session)
    d = date(2026, 11, 20)
    assert save_results(session, i.id, d, {"mom_3m": AlgoResult(60, 5.0, "a")}, None, "r1") == 1
    save_results(session, i.id, d, {"mom_3m": AlgoResult(70, 6.0, "b")}, None, "r2")
    rows = session.query(AlgoScore).filter_by(instrument_id=i.id).all()
    assert len(rows) == 1 and rows[0].score == 70 and rows[0].reason == "b" and rows[0].run_id == "r2"


def test_none_scores_are_stored_with_reason(session):
    i = _inst(session)
    save_results(session, i.id, date(2026, 11, 20), {"pe_ratio": AlgoResult(None, None, "needs EPS")}, None, None)
    row = session.query(AlgoScore).filter_by(instrument_id=i.id).one()
    assert row.score is None and row.reason == "needs EPS"


def test_latest_picks_newest_as_of(session):
    i = _inst(session)
    save_results(session, i.id, date(2026, 11, 19), {"mom_3m": AlgoResult(40, 1.0, "old")}, None, None)
    save_results(session, i.id, date(2026, 11, 20), {"mom_3m": AlgoResult(60, 2.0, "new")}, None, None)
    assert latest_results(session, i.id)["mom_3m"].reason == "new"


def test_score_check_constraint_rejects_101(session):
    i = _inst(session)
    session.add(AlgoScore(instrument_id=i.id, algo_id="x", as_of=date(2026, 11, 20), score=101, reason="r"))
    with pytest.raises(IntegrityError):
        session.flush()


def test_latest_composite_none_without_rows(session):
    i = _inst(session)
    assert latest_composite(session, i.id, {}) is None


def test_latest_composite_means_scores(session):
    i = _inst(session)
    save_results(session, i.id, date(2026, 11, 20),
                 {"mom_6m": AlgoResult(40, 0, ""), "mom_12m_skip_1m": AlgoResult(80, 0, "")}, None, None)
    c = latest_composite(session, i.id, {})
    assert c.long == 60 and c.short is None


def test_peer_momentum_by_macro_sector(session):
    a, b = _inst(session, "INE009A01021"), _inst(session, "INE002A01018")
    other = _inst(session, "INE003A01015", macro="Healthcare")
    for inst, v in ((a, 5.0), (b, 9.0), (other, 1.0)):
        save_results(session, inst.id, date(2026, 11, 20), {"mom_6m": AlgoResult(50, v, "")}, None, None)
    assert peer_momentum(session, "Information Technology") == {"INE009A01021": 5.0, "INE002A01018": 9.0}


def test_migration_0012_up_and_down():
    scratch = "ier_migration_0012_test"
    base = make_url(TEST_URL)
    url = base.set(database=scratch).render_as_string(hide_password=False)
    assert_safe_test_db(url)
    admin = create_engine(base.set(database="postgres").render_as_string(hide_password=False), isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'DROP DATABASE IF EXISTS "{scratch}"'))
        c.execute(text(f'CREATE DATABASE "{scratch}"'))
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    cfg.set_main_option("prepend_sys_path", str(ROOT / "src"))
    prev = os.environ.get("IER_DATABASE_URL")
    os.environ["IER_DATABASE_URL"] = url
    try:
        command.upgrade(cfg, "0011")
        eng = create_engine(url)
        assert "algo_scores" not in inspect(eng).get_table_names()
        eng.dispose()
        command.upgrade(cfg, "0012")
        eng = create_engine(url)
        assert "algo_scores" in inspect(eng).get_table_names()
        cols = {c["name"] for c in inspect(eng).get_columns("scorecards")}
        assert {"rules_long_score", "rules_short_score", "algo_long_score", "algo_short_score", "algo_weight"} <= cols
        eng.dispose()
        command.downgrade(cfg, "0011")
        eng = create_engine(url)
        assert "algo_scores" not in inspect(eng).get_table_names()
        eng.dispose()
        command.upgrade(cfg, "head")
    finally:
        if prev is None:
            os.environ.pop("IER_DATABASE_URL", None)
        else:
            os.environ["IER_DATABASE_URL"] = prev
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{scratch}" WITH (FORCE)'))
        admin.dispose()
