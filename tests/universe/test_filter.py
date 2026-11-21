from datetime import date, timedelta

from ier.db.models import Instrument
from ier.universe.filter import build_universe

TODAY = date(2026, 11, 6)
OLD = TODAY - timedelta(days=2000)
LIQUID = 50_000_000.0
_n = 0


def make(session, **kw):
    global _n
    _n += 1
    d = dict(
        isin=f"INE{_n:09d}", name=f"Co {_n:03d}", macro_sector="Information Technology",
        status="active", surveillance_flag=None, listing_date=OLD, avg_daily_value=LIQUID,
    )
    d.update(kw)
    inst = Instrument(**d)
    session.add(inst)
    session.flush()
    return inst


def names(res):
    return [i.name for i in res]


def test_excludes_suspended(session):
    make(session, name="Ok")
    make(session, name="Susp", status="suspended")
    assert names(build_universe(session, None, today=TODAY)) == ["Ok"]


def test_excludes_surveillance_listed(session):
    make(session, name="Ok")
    make(session, name="Asm", surveillance_flag="ASM")
    assert names(build_universe(session, None, today=TODAY)) == ["Ok"]


def test_excludes_recent_listing(session):
    make(session, name="Ok")
    make(session, name="New", listing_date=TODAY - timedelta(days=100))
    assert names(build_universe(session, None, today=TODAY)) == ["Ok"]
    assert "New" in names(build_universe(session, None, min_listing_days=50, today=TODAY))


def test_filters_by_macro_sector(session):
    make(session, name="IT1")
    make(session, name="Bank", macro_sector="Financial Services")
    assert names(build_universe(session, "Information Technology", today=TODAY)) == ["IT1"]


def test_excludes_illiquid(session):
    make(session, name="Ok")
    make(session, name="Thin", avg_daily_value=9_999_999.0)
    assert names(build_universe(session, None, today=TODAY)) == ["Ok"]


def test_missing_listing_date_excluded(session):
    make(session, name="Ok")
    make(session, name="NoDate", listing_date=None)
    assert names(build_universe(session, None, today=TODAY)) == ["Ok"]


def test_missing_avg_daily_value_excluded(session):
    make(session, name="Ok")
    make(session, name="NoAdv", avg_daily_value=None)
    assert names(build_universe(session, None, today=TODAY)) == ["Ok"]


def test_no_sector_filter_returns_all_eligible(session):
    make(session, name="B", macro_sector="Financial Services")
    make(session, name="A")
    make(session, name="C", macro_sector=None)
    assert names(build_universe(session, None, today=TODAY)) == ["A", "B", "C"]
