"""F7: migration 0010 (plan-job uniqueness + read indexes) up/down on a scratch database."""
import os

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from tests.db.conftest import ROOT, TEST_URL, assert_safe_test_db

SCRATCH = "ier_migration_0010_test"
INDEXES = {"uq_jobs_plan_run", "ix_agent_events_status_latest", "ix_jobs_status_scheduled_at",
           "ix_facts_instrument_metric", "ix_run_log_agent_id", "ix_sources_url"}


def _indexes(url):
    eng = create_engine(url)
    try:
        with eng.connect() as c:
            return set(c.execute(text("select indexname from pg_indexes where schemaname='public'"))
                       .scalars())
    finally:
        eng.dispose()


def _plan_jobs(url, trigger="run", n=2):
    eng = create_engine(url)
    try:
        with eng.begin() as c:
            aid = c.execute(text("insert into agents (tier, role) values (5, 'company') returning id")).scalar_one()
            for _ in range(n):
                c.execute(text("insert into jobs (agent_id, trigger, status, attempts, payload) "
                               "values (:a, :t, 'queued', 0, '{\"run_id\": \"r1\"}')"),
                          {"a": aid, "t": trigger})
    finally:
        eng.dispose()


def test_migration_0010_up_and_down_on_scratch_database():
    base = make_url(TEST_URL)
    url = base.set(database=SCRATCH).render_as_string(hide_password=False)
    assert_safe_test_db(url)
    admin = create_engine(base.set(database="postgres").render_as_string(hide_password=False),
                          isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'DROP DATABASE IF EXISTS "{SCRATCH}"'))
        c.execute(text(f'CREATE DATABASE "{SCRATCH}"'))
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    cfg.set_main_option("prepend_sys_path", str(ROOT / "src"))
    previous = os.environ.get("IER_DATABASE_URL")
    os.environ["IER_DATABASE_URL"] = url
    try:
        command.upgrade(cfg, "0009")
        assert not INDEXES & _indexes(url)
        command.upgrade(cfg, "0010")
        assert INDEXES <= _indexes(url)
        _plan_jobs(url, trigger="manual")            # manual reruns are never blocked
        with pytest.raises(IntegrityError):
            _plan_jobs(url, trigger="run")           # two plan jobs, same agent + run_id
        command.downgrade(cfg, "0009")
        assert not INDEXES & _indexes(url)
        command.upgrade(cfg, "head")
        assert INDEXES <= _indexes(url)
    finally:
        if previous is None:
            os.environ.pop("IER_DATABASE_URL", None)
        else:
            os.environ["IER_DATABASE_URL"] = previous
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{SCRATCH}" WITH (FORCE)'))
        admin.dispose()
