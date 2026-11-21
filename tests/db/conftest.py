import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[2]
TEST_URL = os.environ.get(
    "IER_TEST_DATABASE_URL", "postgresql+psycopg://postgres@localhost:5433/ier_test"
)


def assert_safe_test_db(url: str) -> None:
    name = make_url(url).database or ""
    if not name.endswith("_test"):
        raise RuntimeError(
            f"Refusing to wipe database {name!r}: test database name must end in '_test'"
        )


@pytest.fixture(scope="session")
def engine():
    assert_safe_test_db(TEST_URL)
    eng = create_engine(TEST_URL)
    with eng.begin() as c:
        c.execute(text("DROP SCHEMA public CASCADE"))
        c.execute(text("CREATE SCHEMA public"))
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    cfg.set_main_option("prepend_sys_path", str(ROOT / "src"))
    previous = os.environ.get("IER_DATABASE_URL")
    os.environ["IER_DATABASE_URL"] = TEST_URL
    try:
        command.upgrade(cfg, "head")
    finally:
        if previous is None:
            os.environ.pop("IER_DATABASE_URL", None)
        else:
            os.environ["IER_DATABASE_URL"] = previous
    yield eng
    eng.dispose()


@pytest.fixture
def session(engine):
    conn = engine.connect()
    trans = conn.begin()
    s = Session(conn, join_transaction_mode="create_savepoint")
    yield s
    s.close()
    trans.rollback()
    conn.close()
