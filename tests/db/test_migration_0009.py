import os

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from tests.db.conftest import ROOT, TEST_URL, assert_safe_test_db

SCRATCH = "ier_migration_0009_test"


def _default(url):
    eng = create_engine(url)
    try:
        with eng.connect() as c:
            return c.execute(text(
                "select column_default from information_schema.columns "
                "where table_name='agent_events' and column_name='created_at'")).scalar_one()
    finally:
        eng.dispose()


def test_migration_0009_up_and_down_on_scratch_database():
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
        # the vector extension must exist for the schema; the initial migration creates it
        command.upgrade(cfg, "0008")
        assert "now()" in _default(url)
        command.upgrade(cfg, "0009")
        assert "clock_timestamp()" in _default(url)
        command.downgrade(cfg, "0008")
        assert "now()" in _default(url)
        command.upgrade(cfg, "head")
        assert "clock_timestamp()" in _default(url)
    finally:
        if previous is None:
            os.environ.pop("IER_DATABASE_URL", None)
        else:
            os.environ["IER_DATABASE_URL"] = previous
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{SCRATCH}" WITH (FORCE)'))
        admin.dispose()
