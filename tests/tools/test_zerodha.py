import hashlib
import json
from argparse import Namespace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from ier.agents.technicals import compute_metrics, parse_prices
from ier.db.models import Instrument
from ier.runtime.__main__ import _kite_prices
from ier.tools import zerodha
from ier.tools.zerodha import (IST, KiteClient, KiteError, KiteSessionExpired, candles_csv,
                               fetch_prices, load_token, next_expiry, request_token_from,
                               save_token, settle_candles, write_price_file)

INFY, TCS = "INE009A01021", "INE467B01029"
INSTRUMENTS = (
    "instrument_token,exchange_token,tradingsymbol,name,last_price,expiry,strike,tick_size,lot_size,"
    "instrument_type,segment,exchange\n"
    "408065,1594,INFY,INFOSYS,0,,0,0.05,1,EQ,NSE,NSE\n"
    "2953217,11536,TCS,TATA CONSULTANCY SERVICES,0,,0,0.05,1,EQ,NSE,NSE\n"
    "999999,3900,INFY,INFOSYS,0,,0,0.05,1,EQ,BSE,BSE\n"
)


class Resp:
    def __init__(self, status=200, body=None, text=None):
        self.status_code = status
        self._body = body
        self.text = text if text is not None else json.dumps(body)

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body


class FakeHttp:
    """Stands in for httpx: handler(method, url, params, data) -> Resp. Records every call."""

    def __init__(self, handler):
        self.handler, self.calls = handler, []

    def get(self, url, headers=None, params=None):
        self.calls.append(("GET", url, params))
        return self.handler("GET", url, params, None)

    def post(self, url, headers=None, data=None):
        self.calls.append(("POST", url, data))
        return self.handler("POST", url, None, data)


def candles(n=300, end=date(2026, 11, 13), start_close=100.0):
    out = []
    for i in range(n):
        d = end - timedelta(days=n - 1 - i)
        c = start_close + i * 0.5
        out.append([f"{d.isoformat()}T00:00:00+0530", c - 1, c + 1, c - 2, c, 1000 + i])
    return out


def market(n=300, end=date(2026, 11, 13)):
    def handler(method, url, params, data):
        if url.endswith("/instruments/NSE"):
            return Resp(text=INSTRUMENTS)
        if "/instruments/historical/" in url:
            return Resp(body={"status": "success", "data": {"candles": candles(n, end)}})
        raise AssertionError(url)
    return FakeHttp(handler)


def client_for(http):
    return KiteClient("key12345", "tok12345", http=http, min_gap=0, sleep=lambda s: None)


NOW = datetime(2026, 11, 14, 10, 0, tzinfo=IST)     # a Saturday: the 13th candle is final


# ---- session ------------------------------------------------------------------------------

def test_next_expiry_is_the_next_6am_ist():
    assert next_expiry(datetime(2026, 11, 15, 12, 0, tzinfo=IST)) == datetime(2026, 11, 16, 6, 0, tzinfo=IST)
    assert next_expiry(datetime(2026, 11, 15, 2, 0, tzinfo=IST)) == datetime(2026, 11, 15, 6, 0, tzinfo=IST)
    assert next_expiry(datetime(2026, 11, 15, 0, 0, tzinfo=timezone.utc)) == datetime(2026, 11, 15, 6, 0, tzinfo=IST)


def test_token_round_trip_expiry_and_missing(tmp_path):
    p = tmp_path / "t" / "tok.json"
    now = datetime(2026, 11, 15, 12, 0, tzinfo=IST)
    save_token(p, "key12345", "tok12345", now)
    assert load_token(p, now + timedelta(hours=10)) == ("key12345", "tok12345")
    with pytest.raises(KiteSessionExpired, match="expired"):
        load_token(p, now + timedelta(hours=19))              # after 06:00 the next day
    with pytest.raises(KiteSessionExpired, match="kite-login"):
        load_token(tmp_path / "missing.json", now)
    assert not list((tmp_path / "t").glob("*.part"))


def test_request_token_from_url_or_bare_value():
    url = "https://127.0.0.1/?action=login&type=login&status=success&request_token=AbC123xyz789"
    assert request_token_from(url) == "AbC123xyz789"
    assert request_token_from("  'AbC123xyz789' ") == "AbC123xyz789"
    for bad in ("", "no token here", "https://x/?status=success", "a b c"):
        with pytest.raises(KiteError):
            request_token_from(bad)


def test_login_flow_saves_token_and_never_prints_secrets(tmp_path):
    secret, access = "s3cretSECRET99", "ACCESStoken0000"
    seen = {}

    def handler(method, url, params, data):
        seen.update(data or {})
        return Resp(body={"status": "success", "data": {"access_token": access}})

    out = []
    cfg = {**zerodha.DEFAULTS, "token_file": str(tmp_path / "tok.json")}
    rc = zerodha.login_flow({"IER_KITE_API_KEY": "key12345", "IER_KITE_API_SECRET": secret}, cfg,
                            input_fn=lambda _: "https://127.0.0.1/?request_token=REQtoken1234&status=success",
                            print_fn=out.append, http=FakeHttp(handler),
                            now=datetime(2026, 11, 15, 12, 0, tzinfo=IST))
    assert rc == 0
    assert seen["checksum"] == hashlib.sha256(("key12345" + "REQtoken1234" + secret).encode()).hexdigest()
    assert load_token(tmp_path / "tok.json", datetime(2026, 11, 15, 13, 0, tzinfo=IST)) == ("key12345", access)
    text = "\n".join(out)
    assert secret not in text and access not in text and "16 Nov 2026 06:00" in text
    assert "api_key=key12345" in text                       # the login link carries the key only
    assert zerodha.login_flow({}, cfg, print_fn=out.append) == 1     # no key or secret set


def test_kite_errors_are_translated():
    with pytest.raises(KiteSessionExpired, match="kite-login"):
        zerodha._check(Resp(403, {"status": "error", "error_type": "TokenException", "message": "Incorrect api_key"}))
    with pytest.raises(KiteError, match="paid Kite Connect"):
        zerodha._check(Resp(403, {"status": "error", "error_type": "PermissionException", "message": "Insufficient permission"}))
    with pytest.raises(KiteError, match="HTTP 500"):
        zerodha._check(Resp(500, None, text="oops"))


# ---- read-only ----------------------------------------------------------------------------

def test_client_refuses_anything_but_instruments_and_daily_candles():
    http = FakeHttp(lambda *a: Resp(body={}))
    c = client_for(http)
    for bad in ("/orders/regular", "/portfolio/holdings", "/user/margins", "/instruments/historical/1/minute",
                "/instruments/NFO", "/instruments/historical/abc/day"):
        with pytest.raises(KiteError, match="refused"):
            c._get(bad)
    assert http.calls == []


def test_module_has_no_trading_code_paths():
    src = Path(zerodha.__file__).read_text(encoding="utf-8").lower()
    for word in ("/orders", "/portfolio", "/margins", "/gtt", "place_order", "/mf/", "/positions"):
        assert word not in src


# ---- prices -------------------------------------------------------------------------------

def test_fetch_prices_writes_a_file_the_technicals_agent_can_read(session, tmp_path):
    session.add_all([Instrument(isin=INFY, nse_code="INFY", name="Infosys"),
                     Instrument(isin=TCS, nse_code="TCS", name="TCS")])
    session.flush()
    http = market()
    res = fetch_prices(session, client_for(http), zerodha.DEFAULTS, str(tmp_path), now=NOW,
                       symbols=["INFY", "TCS"])
    assert [(r["symbol"], r["status"], r["rows"], r["asof"]) for r in res] == [
        ("INFY", "written", 300, "2026-11-13"), ("TCS", "written", 300, "2026-11-13")]
    f = tmp_path / INFY / "prices__zerodha__2026-11-13.csv"
    rows = parse_prices(f.read_text())
    assert len(rows) == 300 and rows[-1][0] == date(2026, 11, 13) and rows[-1][1] == 249.5
    m = compute_metrics(rows)
    assert {"price_vs_sma200", "price_vs_sma50", "momentum_3m", "rsi14"} <= set(m)
    hist = [c for c in http.calls if "/historical/" in c[1]]
    assert hist[0][1].endswith("/408065/day")                       # the NSE equity, not the BSE decoy
    assert hist[0][2]["from"] == "2025-10-10 00:00:00" and hist[0][2]["to"] == "2026-11-14 23:59:59"
    assert sum("/instruments/NSE" in c[1] for c in http.calls) == 1     # list fetched once

    again = fetch_prices(session, client_for(market()), zerodha.DEFAULTS, str(tmp_path), now=NOW, symbols=["INFY"])
    assert again[0]["status"] == "unchanged"                        # identical: nothing rewritten
    newer = fetch_prices(session, client_for(market(end=date(2026, 11, 16))), zerodha.DEFAULTS, str(tmp_path),
                         now=NOW + timedelta(days=3), symbols=["INFY"])
    assert newer[0]["status"] == "written"
    assert [p.name for p in (tmp_path / INFY).glob("prices__zerodha__*.csv")] == ["prices__zerodha__2026-11-16.csv"]
    assert not list((tmp_path / INFY).glob("*.part"))


def test_other_price_files_are_left_alone(session, tmp_path):
    session.add(Instrument(isin=INFY, nse_code="INFY", name="Infosys"))
    session.flush()
    (tmp_path / INFY).mkdir()
    mine = tmp_path / INFY / "prices__daily__2026-11-13.csv"
    mine.write_text("Date,Close\n2026-11-13,1141.5\n")
    fetch_prices(session, client_for(market()), zerodha.DEFAULTS, str(tmp_path), now=NOW, symbols=["INFY"])
    assert mine.read_text() == "Date,Close\n2026-11-13,1141.5\n"


def test_unfinished_candle_dropped_before_the_close_unless_asked():
    cs = [(date(2026, 11, 12), 1, 1, 1, 1, 1), (date(2026, 11, 13), 2, 2, 2, 2, 2)]
    before = datetime(2026, 11, 13, 11, 0, tzinfo=IST)
    after = datetime(2026, 11, 13, 16, 0, tzinfo=IST)
    assert [c[0].day for c in settle_candles(cs, before, False)] == [12]
    assert [c[0].day for c in settle_candles(cs, before, True)] == [12, 13]
    assert [c[0].day for c in settle_candles(cs, after, False)] == [12, 13]


def test_per_symbol_problems_are_reported_and_a_dead_session_aborts(session, tmp_path):
    session.add(Instrument(isin=INFY, nse_code="INFY", name="Infosys"))
    session.add(Instrument(isin="INE000000001", nse_code="NOPE", name="Not on Zerodha"))
    session.flush()
    res = fetch_prices(session, client_for(market()), zerodha.DEFAULTS, str(tmp_path), now=NOW,
                       symbols=["INFY", "NOPE", "WIPRO"])
    assert [r["status"] for r in res] == ["written", "error", "error"]
    assert "instrument list" in res[1]["error"] and "instruments table" in res[2]["error"]

    def expired(method, url, params, data):
        if url.endswith("/instruments/NSE"):
            return Resp(text=INSTRUMENTS)
        return Resp(403, {"status": "error", "error_type": "TokenException", "message": "Incorrect api_key or access_token"})
    with pytest.raises(KiteSessionExpired):
        fetch_prices(session, client_for(FakeHttp(expired)), zerodha.DEFAULTS, str(tmp_path), now=NOW, symbols=["INFY"])
    with pytest.raises(KiteError, match="valid NSE symbol"):
        fetch_prices(session, client_for(market()), zerodha.DEFAULTS, str(tmp_path), now=NOW, symbols=["../x"])


def test_write_price_file_stays_inside_local_dir(tmp_path):
    csv_text = candles_csv([(date(2026, 11, 13), 1, 2, 0.5, 1.5, 10), (date(2026, 11, 12), 1, 1, 1, 1, 1)])
    with pytest.raises(KiteError, match="ISIN"):
        write_price_file(str(tmp_path), "../evil", date(2026, 11, 13), csv_text)
    with pytest.raises(KiteError, match="not a folder"):
        write_price_file(str(tmp_path / "nope"), INFY, date(2026, 11, 13), csv_text)
    assert write_price_file(str(tmp_path), INFY, date(2026, 11, 13), csv_text) == "written"


def test_kite_prices_command_prints_one_line_per_company(session, tmp_path, monkeypatch, capsys):
    session.add_all([Instrument(isin=INFY, nse_code="INFY", name="Infosys"),
                     Instrument(isin=TCS, nse_code="TCS", name="TCS")])
    session.flush()
    monkeypatch.setattr("ier.tools.exchange_filings.load_config", lambda *a, **k: {"local_dir": str(tmp_path)})
    args = Namespace(symbols="INFY,TCS,WIPRO", days=None, intraday=False, loop=None)
    rc = _kite_prices(args, lambda: session, client=client_for(market()), now_fn=lambda: NOW)
    out = capsys.readouterr().out
    assert rc == 1                                                   # WIPRO is not in the table
    assert "INFY" in out and "written" in out and "300 rows, as of 2026-11-13" in out
    assert "WIPRO" in out and "not in the instruments table" in out
    with pytest.raises(SystemExit, match="at least 5"):
        _kite_prices(Namespace(symbols=None, days=None, intraday=False, loop=1.0), lambda: session)
