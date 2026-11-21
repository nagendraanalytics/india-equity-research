import time

import pytest
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from ier.db.models import Base
from tests.db.conftest import engine, session  # noqa: F401


def _truncate(eng):
    names = ", ".join(t.name for t in Base.metadata.sorted_tables)
    with eng.begin() as c:
        c.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))


@pytest.fixture
def factory(engine):  # noqa: F811
    """Sessions on separate connections that really commit; tables wiped before and after."""
    _truncate(engine)
    yield sessionmaker(engine, expire_on_commit=False)
    _truncate(engine)


def wait_for(pred, timeout=5.0, interval=0.01):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return pred()
