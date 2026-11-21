from datetime import date, datetime, timedelta, timezone

import httpx
import pytest

from ier.agents.technicals import parse_prices
from ier.db.models import Instrument
from ier.tools import nse_prices as nse
from ier.tools.nse_prices import NseClient, NseError

INFY, TCS = "INE009A01021", "INE467B01029"
NOW = datetime(2026, 11, 20, 13, 30, tzinfo=timezone.utc)      # Fri 19:00 IST: the day's file is out
HEADER = "SYMBOL, SERIES, DATE1, PREV_CLOSE, OPEN_PRICE, HIGH_PRICE, LOW_PRICE, LAST_PRICE, CLOSE_PRICE, AVG_PRICE, TTL_TRD_QNTY, TURNOVER_LACS, NO_OF_TRADES, DELIV_QTY, DELIV_PER"


def day_text(d: date, symbols=("INFY", "TCS")) -> str:
    stamp = d.strftime("%d-%b-%Y")
    rows = [HEADER]
    for i, s in enumerate(symbols):
        c = 1000 + i * 500 + d.toordinal() % 50
        rows.append(f"{s}, EQ, {stamp}, {c - 1}.00, {c - 2}.00, {c + 5}.00, {c - 6}.00, {c}.00, {c}.00, {c}.00, {100000 + i}, 1.0, 10, 1, 1.0")
    rows.append(f"INFY, BE, {stamp}, 1.00, 1.00, 1.00, 1.00, 1.00, 1.00, 1.00, 5, 1.0, 1, 1, 1.0")   # other series: ignored
    rows.append(f"OTHER, EQ, {stamp}, 9.00, 9.00, 9.00, 9.00, 9.00, 9.00, 9.00, 5, 1.0, 1, 1, 1.0")     # untracked: ignored
    return "\n".join(rows) + "\n"


def client(holidays=(), status=None, log=None, symbols=("INFY", "TCS"), location=None):
    def handler(req: httpx.Request) -> httpx.Response:
        if log is not None:
            log.append(req.url.path)
        if location:
            return httpx.Response(302, headers={"location": location})
        if status:
            return httpx.Response(status)
        stamp = req.url.path.rsplit("_", 1)[1].split(".")[0]
        d = datetime.strptime(stamp, "%d%m%Y").date()
        if d in holidays or d.weekday() >= 5:
            return httpx.Response(404)
        return httpx.Response(200, text=day_text(d, symbols))
    return NseClient(transport=httpx.MockTransport(handler), min_gap=0, sleep=lambda s: None)


def test_parse_keeps_only_tracked_equity_rows_and_strips_header_spaces():
    got = nse.parse_bhavcopy(day_text(date(2026, 11, 20)), {"INFY"})
    assert set(got) == {"INFY"}
    d, o, h, lo, c, v = got["INFY"]
    assert d == date(2026, 11, 20) and v == 100000 and h > c > lo


def test_trading_days_skip_weekends_and_latest_published_respects_publish_time():
    assert [d.isoformat() for d in nse.trading_days(date(2026, 11, 19), date(2026, 11, 24))] == [
        "2026-11-19", "2026-11-20", "2026-11-23", "2026-11-24"]
    cfg = nse.DEFAULTS
    assert nse.latest_published(NOW, cfg) == date(2026, 11, 20)
    assert nse.latest_published(NOW.replace(hour=9), cfg) == date(2026, 11, 19)     # 14:30 IST: not published yet
    assert nse.latest_published(NOW + timedelta(days=1), cfg) == date(2026, 11, 20)  # Saturday -> Friday


def test_update_writes_a_file_the_algorithms_can_read_then_only_fetches_new_days(session, tmp_path):
    session.add_all([Instrument(isin=INFY, nse_code="INFY", name="Infosys"), Instrument(isin=TCS, nse_code="TCS", name="TCS")])
    session.flush()
    log: list[str] = []
    res = nse.update_prices(session, client(holidays={date(2026, 11, 10)}, log=log), nse.DEFAULTS, str(tmp_path), now=NOW, days=14)
    assert {r["symbol"]: r["status"] for r in res} == {"INFY": "written", "TCS": "written"}
    assert all("sec_bhavdata_full_" in p for p in log) and len(log) == len(set(log))     # one fetch per day, shared by companies
    f = tmp_path / INFY / "prices__nse__2026-11-20.csv"
    rows = parse_prices(f.read_text())
    assert rows[-1][0] == date(2026, 11, 20) and date(2026, 11, 10) not in [r[0] for r in rows] and rows[0][2] is not None

    log.clear()
    again = nse.update_prices(session, client(log=log), nse.DEFAULTS, str(tmp_path), now=NOW, days=14)
    assert {r["status"] for r in again} == {"current"} and log == []                       # nothing missing: no network

    log.clear()
    later = nse.update_prices(session, client(log=log), nse.DEFAULTS, str(tmp_path), now=NOW + timedelta(days=3), days=14)
    assert [p.rsplit("_", 1)[1] for p in log] == ["23112026.csv"]                        # only the new Monday
    assert {r["status"] for r in later} == {"written"}
    assert (tmp_path / INFY / "prices__nse__2026-11-23.csv").exists() and not f.exists()  # one file per company


def test_symbol_missing_from_the_exchange_files_is_reported_not_invented(session, tmp_path):
    session.add(Instrument(isin=INFY, nse_code="INFY", name="Infosys"))
    session.flush()
    res = nse.update_prices(session, client(symbols=("TCS",)), nse.DEFAULTS, str(tmp_path), now=NOW, days=7)
    assert res[0]["status"] == "nodata" and not list(tmp_path.glob("*/*"))


def test_blocked_or_redirected_requests_stop_the_run_with_a_clear_message(session, tmp_path):
    session.add(Instrument(isin=INFY, nse_code="INFY", name="Infosys"))
    session.flush()
    with pytest.raises(NseError, match="403"):
        nse.update_prices(session, client(status=403), nse.DEFAULTS, str(tmp_path), now=NOW, days=7)
    with pytest.raises(NseError, match="redirect"):
        nse.update_prices(session, client(location="https://evil.example/x.csv"), nse.DEFAULTS, str(tmp_path), now=NOW, days=7)


def test_only_the_nse_archive_host_is_ever_requested():
    assert nse.bhavcopy_url(date(2026, 11, 20)) == "https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_20112026.csv"


def test_write_refuses_a_bad_isin_and_a_symlinked_folder(tmp_path):
    with pytest.raises(NseError):
        nse.write_history(str(tmp_path), "../etc", {date(2026, 11, 20): (date(2026, 11, 20), 1, 1, 1, 1, 1)})
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / INFY).symlink_to(tmp_path / "elsewhere")
    with pytest.raises(NseError):
        nse.write_history(str(tmp_path), INFY, {date(2026, 11, 20): (date(2026, 11, 20), 1, 1, 1, 1, 1)})


def test_cli_prints_one_line_per_company_and_flags_unknown_symbols(session, tmp_path, monkeypatch, capsys):
    from argparse import Namespace

    from ier.runtime.__main__ import _nse_prices
    session.add(Instrument(isin=INFY, nse_code="INFY", name="Infosys"))
    session.flush()
    monkeypatch.setattr("ier.tools.exchange_filings.load_config", lambda *a, **k: {"local_dir": str(tmp_path)})
    args = Namespace(symbols="INFY,WIPRO", days=7, loop=None)
    rc = _nse_prices(args, lambda: session, client=client(), now_fn=lambda: NOW)
    out = capsys.readouterr().out
    assert rc == 1
    assert "INFY" in out and "written" in out and "as of 2026-11-20" in out
    assert "WIPRO" in out and "not in the instruments table" in out
    with pytest.raises(SystemExit, match="at least 5"):
        _nse_prices(Namespace(symbols=None, days=None, loop=1.0), lambda: session)
