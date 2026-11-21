from datetime import date, timedelta

import pytest

from ier.algos.core import Bar, Ctx


def make_bars(closes, volumes=None, start=date(2025, 1, 1)):
    return [Bar(start + timedelta(days=i), float(c), None if volumes is None else float(volumes[i]))
            for i, c in enumerate(closes)]


def make_ctx(closes, volumes=None, **kw):
    return Ctx(bars=make_bars(closes, volumes), cfg=kw.pop("cfg", {}), isin="INE009A01021", **kw)


@pytest.fixture
def ctx_factory():
    return make_ctx
from tests.db.conftest import engine, session  # noqa: F401  (DB fixtures)
