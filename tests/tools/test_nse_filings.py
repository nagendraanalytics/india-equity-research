from datetime import date

from ier.tools import nse_filings as nf


def test_quarter_label_indian_fy():
    assert nf.quarter_label(date(2026, 3, 31)) == "Q4FY26"
    assert nf.quarter_label(date(2026, 6, 30)) == "Q1FY27"
    assert nf.quarter_label(date(2026, 9, 30)) == "Q2FY27"
    assert nf.quarter_label(date(2026, 12, 31)) == "Q3FY27"


def test_fy_label():
    assert nf.fy_label(date(2026, 3, 31)) == "FY2026"
    assert nf.fy_label(date(2025, 3, 31)) == "FY2025"


def test_filing_name():
    assert nf.filing_name("results", "Q2FY27", date(2026, 10, 16)) == "results__Q2FY27__2026-10-16.pdf"
    assert nf.filing_name("annual_report", "FY2026", date(2026, 7, 23)) == "annual_report__FY2026__2026-07-23.pdf"


def test_host_allowed_rejects_http_other_hosts_and_lookalikes():
    cfg = nf.load_config()
    assert nf.host_allowed("https://nsearchives.nseindia.com/corporate/x.pdf", cfg)
    assert nf.host_allowed("https://archives.nseindia.com/annual_reports/x.pdf", cfg)
    assert not nf.host_allowed("http://nsearchives.nseindia.com/x.pdf", cfg)
    assert not nf.host_allowed("https://evil.example/x.pdf", cfg)
    assert not nf.host_allowed("https://nsearchives.nseindia.com.evil.com/x.pdf", cfg)
    assert not nf.host_allowed("not a url", cfg)


def test_looks_like_pdf():
    assert nf.looks_like_pdf(b"%PDF-1.7\n...")
    assert not nf.looks_like_pdf(b"<html>blocked</html>")
    assert not nf.looks_like_pdf(b"")


def test_load_config_defaults():
    cfg = nf.load_config()
    assert cfg["results_years"] == 2 and cfg["annual_years"] == 3
    assert cfg["min_gap_seconds"] >= 1.0
    assert "www.nseindia.com" in cfg["allowed_hosts"]


# --- Task 2: client ---------------------------------------------------------------------------

import httpx
import pytest

from ier.tools.nse_filings import NseBlocked, NseError, NseFilingsClient

PDF = b"%PDF-1.7\n" + b"x" * 100


def mk(handler, **kw):
    cfg = {**nf.load_config(), "min_gap_seconds": 0, **kw}
    sleeps: list[float] = []
    c = NseFilingsClient(cfg, transport=httpx.MockTransport(handler), sleep=sleeps.append)
    return c, sleeps


def test_session_opened_once_then_json():
    seen: list[str] = []

    def h(req):
        seen.append(req.url.path)
        return httpx.Response(200, json=[{"a": 1}]) if req.url.path.startswith("/api") else httpx.Response(200, text="home")
    c, _ = mk(h)
    assert c.get_json("/api/x?symbol=ITC") == [{"a": 1}]
    assert c.get_json("/api/y?symbol=ITC") == [{"a": 1}]
    assert seen == ["/", "/api/x", "/api/y"]


def test_403_raises_blocked():
    c, _ = mk(lambda req: httpx.Response(403))
    with pytest.raises(NseBlocked, match="403"):
        c.get_json("/api/x")


def test_html_with_200_raises_blocked():
    c, _ = mk(lambda req: httpx.Response(200, text="<html>Access Denied</html>"))
    with pytest.raises(NseBlocked, match="page instead of data"):
        c.get_json("/api/x")


def test_retries_5xx_then_error():
    calls = {"n": 0}

    def h(req):
        if req.url.path == "/":
            return httpx.Response(200, text="home")
        calls["n"] += 1
        return httpx.Response(503)
    c, _ = mk(h)
    with pytest.raises(NseError, match="503"):
        c.get_json("/api/x")
    assert calls["n"] == 3        # first try plus two retries


def test_download_ok_pdf():
    c, _ = mk(lambda req: httpx.Response(200, content=PDF))
    assert c.download("https://nsearchives.nseindia.com/corporate/a.pdf", 1_000_000) == PDF


def test_download_html_body_returns_none():
    c, _ = mk(lambda req: httpx.Response(200, content=b"<html>nope</html>"))
    assert c.download("https://nsearchives.nseindia.com/corporate/a.pdf", 1_000_000) is None


def test_download_off_list_host_not_requested():
    seen: list[str] = []
    c, _ = mk(lambda req: seen.append(str(req.url)) or httpx.Response(200, content=PDF))
    assert c.download("https://evil.example/a.pdf", 1_000_000) is None
    assert seen == []


def test_download_redirect_refused():
    c, _ = mk(lambda req: httpx.Response(302, headers={"location": "https://evil.example/a.pdf"}))
    with pytest.raises(NseError, match="redirect"):
        c.download("https://nsearchives.nseindia.com/corporate/a.pdf", 1_000_000)


def test_download_too_large_returns_none():
    c, _ = mk(lambda req: httpx.Response(200, content=PDF))
    assert c.download("https://nsearchives.nseindia.com/corporate/a.pdf", 50) is None


def test_min_gap_uses_injected_sleep():
    def h(req):
        return httpx.Response(200, json=[]) if req.url.path.startswith("/api") else httpx.Response(200, text="home")
    c, sleeps = mk(h, min_gap_seconds=5)
    c.get_json("/api/x")
    c.get_json("/api/y")
    assert sleeps and max(sleeps) > 0


# --- Task 3: update_filings ---------------------------------------------------------------------

import os
from datetime import datetime, timezone

from ier.db.models import Instrument
from tests.tools.conftest import session  # noqa: F401

ITC, INFY = "INE154A01025", "INE009A01021"
NOW = datetime(2026, 11, 21, 10, 0, tzinfo=timezone.utc)
CFG = {**nf.load_config(), "min_gap_seconds": 0}
RES_URL = "https://nsearchives.nseindia.com/corporate/ITC_results_Q2.pdf"
AR_URL = "https://archives.nseindia.com/annual_reports/AR_ITC_2026.pdf"


class FakeClient:
    def __init__(self, results=None, annual=None, blocked=False, pdfs=None, fail_symbols=()):
        self.results = results if results is not None else [
            {"toDate": "30-Sep-2026", "broadcastDate": "16-Oct-2026 14:25:10", "attachment": RES_URL},
            {"toDate": "30-Jun-2026", "broadcastDate": "20-Jul-2026", "attachment": RES_URL.replace("Q2", "Q1")},
            {"toDate": "30-Jun-2023", "broadcastDate": "20-Jul-2023", "attachment": RES_URL.replace("Q2", "old")},   # too old
        ]
        self.annual = annual if annual is not None else {"data": [
            {"toYr": "2026", "broadcastDate": "23-Jul-2026", "fileName": AR_URL}]}
        self.blocked, self.pdfs, self.fail_symbols = blocked, pdfs, set(fail_symbols)
        self.json_calls: list[str] = []
        self.downloads: list[str] = []

    def get_json(self, path):
        self.json_calls.append(path)
        if self.blocked:
            raise NseBlocked("NSE refused the request (HTTP 403)")
        if any(f"symbol={s}" in path for s in self.fail_symbols):
            raise NseError("NSE returned HTTP 500")
        return self.annual if "annual-reports" in path else self.results

    def download(self, url, max_bytes):
        self.downloads.append(url)
        if self.pdfs is not None:
            return self.pdfs.get(url)
        return PDF


def seed(session, with_infy=False):
    session.add(Instrument(isin=ITC, nse_code="ITC", name="ITC Ltd"))
    if with_infy:
        session.add(Instrument(isin=INFY, nse_code=None, name="No code Ltd"))
    session.flush()


def by_kind(res):
    return {(r["symbol"], r["kind"]): r for r in res}


def test_writes_results_and_annual_files_with_expected_names(session, tmp_path):
    seed(session)
    res = nf.update_filings(session, FakeClient(), CFG, str(tmp_path), now=NOW)
    got = by_kind(res)
    assert got[("ITC", "results")]["status"] == "written" and got[("ITC", "results")]["files"] == 2
    assert got[("ITC", "annual")]["status"] == "written" and got[("ITC", "annual")]["files"] == 1
    names = sorted(p.name for p in (tmp_path / ITC).iterdir())
    assert names == ["annual_report__FY2026__2026-07-23.pdf",
                     "results__Q1FY27__2026-07-20.pdf", "results__Q2FY27__2026-10-16.pdf"]
    assert (tmp_path / ITC / names[0]).read_bytes() == PDF


def test_keeps_only_last_n_years(session, tmp_path):
    seed(session)
    nf.update_filings(session, FakeClient(), CFG, str(tmp_path), now=NOW, kinds=("results",))
    assert not any("2023" in p.name for p in (tmp_path / ITC).iterdir())


def test_second_run_reports_unchanged_and_downloads_nothing(session, tmp_path):
    seed(session)
    nf.update_filings(session, FakeClient(), CFG, str(tmp_path), now=NOW)
    c = FakeClient()
    again = nf.update_filings(session, c, CFG, str(tmp_path), now=NOW)
    assert {r["status"] for r in again} == {"unchanged"} and c.downloads == []


def test_existing_file_is_not_overwritten(session, tmp_path):
    seed(session)
    f = tmp_path / ITC
    f.mkdir()
    (f / "results__Q2FY27__2026-10-16.pdf").write_bytes(b"%PDF-mine")
    nf.update_filings(session, FakeClient(), CFG, str(tmp_path), now=NOW, kinds=("results",))
    assert (f / "results__Q2FY27__2026-10-16.pdf").read_bytes() == b"%PDF-mine"


def test_revised_filing_with_a_new_date_does_not_duplicate_a_period(session, tmp_path):
    seed(session)
    f = tmp_path / ITC
    f.mkdir()
    (f / "results__Q2FY27__2026-10-01.pdf").write_bytes(PDF)
    nf.update_filings(session, FakeClient(), CFG, str(tmp_path), now=NOW, kinds=("results",))
    assert len(list(f.glob("results__Q2FY27__*.pdf"))) == 1


def test_unknown_symbol_is_error_others_continue(session, tmp_path):
    seed(session)
    res = nf.update_filings(session, FakeClient(), CFG, str(tmp_path), now=NOW, symbols=["ITC", "NOPE"])
    got = by_kind(res)
    assert got[("NOPE", "results")]["status"] == "error" and "instruments" in got[("NOPE", "results")]["error"]
    assert got[("ITC", "results")]["status"] == "written"


def test_instrument_without_nse_code_is_skipped(session, tmp_path):
    seed(session, with_infy=True)
    res = nf.update_filings(session, FakeClient(), CFG, str(tmp_path), now=NOW)
    assert {r["isin"] for r in res} == {ITC}


def test_off_list_url_skipped_and_reported(session, tmp_path):
    seed(session)
    c = FakeClient(results=[{"toDate": "30-Sep-2026", "broadcastDate": "16-Oct-2026", "attachment": "https://evil.example/a.pdf"}])
    res = by_kind(nf.update_filings(session, c, CFG, str(tmp_path), now=NOW, kinds=("results",)))
    assert res[("ITC", "results")]["status"] == "error" and "not downloadable" in res[("ITC", "results")]["error"]
    assert c.downloads == [] and not (tmp_path / ITC).exists()


def test_html_body_never_saved(session, tmp_path):
    seed(session)
    c = FakeClient(pdfs={})                      # download returns None for every URL
    res = by_kind(nf.update_filings(session, c, CFG, str(tmp_path), now=NOW, kinds=("results",)))
    assert res[("ITC", "results")]["status"] == "error"
    assert not list(tmp_path.glob("*/*"))


def test_blocked_aborts_rest_and_marks_blocked(session, tmp_path):
    seed(session)
    c = FakeClient(blocked=True)
    res = nf.update_filings(session, c, CFG, str(tmp_path), now=NOW)
    assert {r["status"] for r in res} == {"blocked"} and len(c.json_calls) == 1
    assert {r["kind"] for r in res} == {"results", "annual"}


def test_listing_error_is_reported_for_that_company_only(session, tmp_path):
    seed(session)
    session.add(Instrument(isin=INFY, nse_code="INFY", name="Infosys"))
    session.flush()
    c = FakeClient(fail_symbols=("INFY",))
    got = by_kind(nf.update_filings(session, c, CFG, str(tmp_path), now=NOW, kinds=("results",)))
    assert got[("INFY", "results")]["status"] == "error"
    assert got[("ITC", "results")]["status"] == "written"


def test_nodata_when_nse_lists_nothing(session, tmp_path):
    seed(session)
    res = by_kind(nf.update_filings(session, FakeClient(results=[], annual=[]), CFG, str(tmp_path), now=NOW))
    assert {r["status"] for r in res.values()} == {"nodata"}


def test_write_is_atomic_no_tmp_left_after_failure(session, tmp_path, monkeypatch):
    seed(session)

    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(os, "replace", boom)
    res = by_kind(nf.update_filings(session, FakeClient(), CFG, str(tmp_path), now=NOW, kinds=("results",)))
    assert res[("ITC", "results")]["status"] == "error"
    assert not [p for p in (tmp_path / ITC).iterdir()] if (tmp_path / ITC).exists() else True


def test_parse_items_tolerates_field_names_and_skips_incomplete():
    raw = [{"qe_Date": "2026-09-30", "an_dt": "2026-10-16", "xbrl": "https://nsearchives.nseindia.com/a.pdf"},
           {"toDate": "30-Sep-2026", "broadcastDate": "16-Oct-2026"},                                    # no url
           {"attachment": "https://nsearchives.nseindia.com/b.pdf", "broadcastDate": "16-Oct-2026"}]    # no period
    items = nf.parse_items("results", raw)
    assert items == [{"period": "Q2FY27", "published": date(2026, 10, 16), "url": "https://nsearchives.nseindia.com/a.pdf"}]


# --- Task 4: CLI --------------------------------------------------------------------------------

from argparse import Namespace


def run_cli(session, tmp_path, monkeypatch, capsys, client, **over):
    from ier.runtime.__main__ import _nse_filings
    monkeypatch.setattr("ier.tools.exchange_filings.load_config", lambda *a, **k: {"local_dir": str(tmp_path / "filings")})
    args = Namespace(**{"symbols": None, "kind": "all", "loop": None, **over})
    rc = _nse_filings(args, lambda: session, client=client, now_fn=lambda: NOW)
    return rc, capsys.readouterr().out


def test_cli_prints_one_line_per_company_and_kind(session, tmp_path, monkeypatch, capsys):
    seed(session)
    rc, out = run_cli(session, tmp_path, monkeypatch, capsys, FakeClient())
    assert rc == 0
    lines = [ln for ln in out.splitlines() if ln.startswith("ITC")]
    assert len(lines) == 2 and any("results" in ln and "written" in ln for ln in lines)
    assert any("annual" in ln and "written" in ln for ln in lines)


def test_cli_exit_code_1_when_blocked(session, tmp_path, monkeypatch, capsys):
    seed(session)
    rc, out = run_cli(session, tmp_path, monkeypatch, capsys, FakeClient(blocked=True))
    assert rc == 1 and "blocked" in out


def test_cli_kind_results_only_requests_results(session, tmp_path, monkeypatch, capsys):
    seed(session)
    c = FakeClient()
    run_cli(session, tmp_path, monkeypatch, capsys, c, kind="results")
    assert c.json_calls and all("annual-reports" not in p for p in c.json_calls)


def test_cli_loop_below_5_rejected(session, tmp_path, monkeypatch, capsys):
    seed(session)
    with pytest.raises(SystemExit):
        run_cli(session, tmp_path, monkeypatch, capsys, FakeClient(), loop=2)


def test_cli_parser_knows_the_command():
    from ier.runtime.__main__ import _parser
    a = _parser().parse_args(["nse-filings", "--symbols", "ITC", "--kind", "annual"])
    assert a.command == "nse-filings" and a.symbols == "ITC" and a.kind == "annual" and a.loop is None


# --- home-PC mode: no database, few dependencies ------------------------------------------------

def test_update_for_pairs_needs_no_database(tmp_path):
    res = nf.update_filings_for({"ITC": ITC}, FakeClient(), CFG, str(tmp_path), now=NOW)
    assert {r["status"] for r in res} == {"written"}
    assert (tmp_path / ITC / "annual_report__FY2026__2026-07-23.pdf").exists()


def test_module_imports_without_sqlalchemy_or_the_ier_database_layer():
    import subprocess
    import sys
    from pathlib import Path
    src = str(Path(nf.__file__).resolve().parents[2])
    code = ("import sys; sys.modules['sqlalchemy'] = None; sys.path.insert(0, %r); "
            "import ier.tools.nse_filings as m; assert 'ier.db.models' not in sys.modules; print('ok')" % src)
    out = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True)
    assert out.stdout.strip() == "ok", out.stderr


def test_standalone_main_writes_to_out_and_prints_lines(tmp_path, capsys):
    rc = nf.main(["--isin", f"ITC={ITC}", "--out", str(tmp_path / "out"), "--kind", "results"],
                 client=FakeClient(), now=NOW)
    assert rc == 0
    assert any(p.name.startswith("results__") for p in (tmp_path / "out" / ITC).iterdir())
    assert "ITC" in capsys.readouterr().out


def test_standalone_main_rejects_bad_input(tmp_path):
    for argv in (["--isin", "ITC", "--out", str(tmp_path)],                 # not SYMBOL=ISIN
                 ["--isin", "ITC=../etc", "--out", str(tmp_path)],           # invalid ISIN
                 ["--isin", f"ITC={ITC}"]):                                  # no --out
        with pytest.raises(SystemExit):
            nf.main(argv, client=FakeClient(), now=NOW)


def test_standalone_main_exit_1_when_blocked(tmp_path, capsys):
    rc = nf.main(["--isin", f"ITC={ITC}", "--out", str(tmp_path)], client=FakeClient(blocked=True), now=NOW)
    assert rc == 1 and "blocked" in capsys.readouterr().out
