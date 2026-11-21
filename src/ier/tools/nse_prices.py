"""NSE end-of-day price history for the algorithm library.

Downloads the exchange's public daily "security-wise bhavcopy" files (one file per trading day,
the whole market), keeps the rows of the companies we track (EQ series) and writes one price CSV per
company: <local_dir>/<ISIN>/prices__nse__<last-date>.csv. The technicals step and every algorithm read
that file like any other price file. Only days a company is missing are downloaded, so the first run
fetches the history (about 290 trading days) and each later run fetches one file.

UNVERIFIED, like the filings connector: the owner must confirm that NSE's terms of use allow this use
and that the URL scheme below is still current. NSE may block data-centre addresses; if the server gets
HTTP 403, run the command from a home connection and copy the folders to the server. Read-only; the
only host ever requested is nsearchives.nseindia.com, and redirects are refused.
"""
from __future__ import annotations

import csv
import io
import os
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx
import yaml

from ier.agents.technicals import _date, _num
from ier.tools.local_filings import ISIN_RE

ROOT = Path(__file__).resolve().parents[3]
CONFIG_PATH = ROOT / "config" / "nse_prices.yaml"
HOST = "nsearchives.nseindia.com"
URL = "https://" + HOST + "/products/content/sec_bhavdata_full_{ddmmyyyy}.csv"
IST = timezone(timedelta(hours=5, minutes=30))
DEFAULTS = {
    "days": 400,                  # calendar days of history for a company with none (200-day average needs ~290)
    "min_gap_seconds": 0.6,       # PLACEHOLDER: polite gap between requests
    "publish_after": "18:30",     # PLACEHOLDER: IST time after which the day's file is normally out
    "user_agent": "india-equity-research/1.0 (personal research; read-only)",
    "timeout_seconds": 30,
}
Row = tuple  # (date, open, high, low, close, volume)


class NseError(Exception):
    pass


def load_config(path: Path = CONFIG_PATH) -> dict:
    cfg = dict(DEFAULTS)
    if path.exists():
        cfg.update(yaml.safe_load(path.read_text(encoding="utf-8")) or {})
    return cfg


def bhavcopy_url(day: date) -> str:
    return URL.format(ddmmyyyy=day.strftime("%d%m%Y"))


def trading_days(start: date, end: date) -> list[date]:
    """Weekdays from start to end inclusive; exchange holidays are discovered as missing files."""
    out, d = [], start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def latest_published(now: datetime, cfg: dict) -> date:
    local = now.astimezone(IST)
    hh, mm = (int(x) for x in str(cfg["publish_after"]).split(":"))
    d = local.date()
    if (local.hour, local.minute) < (hh, mm):
        d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def parse_bhavcopy(text: str, symbols: set[str]) -> dict[str, Row]:
    rows = list(csv.reader(io.StringIO(text.lstrip("﻿"))))
    if not rows:
        return {}
    head = [h.strip().lower() for h in rows[0]]
    need = ("symbol", "series", "date1", "open_price", "high_price", "low_price", "close_price", "ttl_trd_qnty")
    if any(n not in head for n in need):
        raise NseError("unexpected file layout: bhavcopy columns changed")
    ix = {n: head.index(n) for n in need}
    out: dict[str, Row] = {}
    for r in rows[1:]:
        if len(r) <= max(ix.values()):
            continue
        r = [c.strip() for c in r]
        if r[ix["series"]] != "EQ" or r[ix["symbol"]] not in symbols:
            continue
        d = _date(r[ix["date1"]])
        o, h, lo, c, v = (_num(r[ix[k]]) for k in ("open_price", "high_price", "low_price", "close_price", "ttl_trd_qnty"))
        if d is None or c is None or c <= 0:
            continue
        out[r[ix["symbol"]]] = (d, o or c, h or c, lo or c, c, int(v or 0))
    return out


class NseClient:
    def __init__(self, *, transport: httpx.BaseTransport | None = None, min_gap: float = 0.6,
                 user_agent: str = DEFAULTS["user_agent"], timeout: float = 30, sleep=time.sleep):
        self._http = httpx.Client(transport=transport, headers={"User-Agent": user_agent}, timeout=timeout, follow_redirects=False)
        self._gap, self._sleep, self._last = min_gap, sleep, 0.0

    def day_file(self, day: date) -> str | None:
        """The day's file text, or None when NSE has no file (holiday). Anything else unusual raises."""
        wait = self._gap - (time.monotonic() - self._last)
        if wait > 0:
            self._sleep(wait)
        self._last = time.monotonic()
        try:
            r = self._http.get(bhavcopy_url(day))
        except httpx.HTTPError as exc:
            raise NseError(f"could not reach {HOST}: {exc.__class__.__name__}") from None
        if r.status_code == 404:
            return None
        if 300 <= r.status_code < 400:
            raise NseError(f"redirect refused for {day}: only {HOST} is allowed")
        if r.status_code != 200:
            raise NseError(f"NSE returned HTTP {r.status_code} for {day}; if this is 403 the address is blocked, "
                           "run the command from a home connection and copy the folders to the server")
        if not r.text.lstrip().upper().startswith("SYMBOL"):
            raise NseError(f"unexpected content for {day}: not a bhavcopy file")
        return r.text


def _folder(local_dir: str, isin: str) -> Path:
    if not ISIN_RE.fullmatch(isin or ""):
        raise NseError(f"not an ISIN: {isin!r}")
    root = Path(local_dir).expanduser().resolve()
    if not root.is_dir():
        raise NseError(f"local_dir {str(root)!r} is not a folder")
    folder = root / isin
    folder.mkdir(exist_ok=True)
    if folder.is_symlink() or not folder.resolve().is_relative_to(root):
        raise NseError(f"refusing to write outside local_dir: {folder}")
    return folder


def read_history(local_dir: str, isin: str) -> dict[date, Row]:
    root = Path(local_dir).expanduser().resolve()
    folder = root / isin
    out: dict[date, Row] = {}
    if not ISIN_RE.fullmatch(isin or "") or not folder.is_dir() or folder.is_symlink():
        return out
    for f in sorted(folder.glob("prices__nse__*.csv")):
        for r in csv.DictReader(io.StringIO(f.read_text(encoding="utf-8"))):
            d = _date(r.get("Date", ""))
            vals = [_num(r.get(k, "")) for k in ("Open", "High", "Low", "Close", "Volume")]
            if d is not None and None not in vals:
                out[d] = (d, vals[0], vals[1], vals[2], vals[3], int(vals[4]))
    return out


def write_history(local_dir: str, isin: str, rows: dict[date, Row]) -> str:
    """Write prices__nse__<last>.csv atomically; 'written' or 'unchanged'. Keeps one file per company."""
    folder = _folder(local_dir, isin)
    last = max(rows)
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["Date", "Open", "High", "Low", "Close", "Volume"])
    for d in sorted(rows):
        _, o, h, lo, c, v = rows[d]
        w.writerow([d.isoformat(), f"{o:.2f}", f"{h:.2f}", f"{lo:.2f}", f"{c:.2f}", v])
    data = buf.getvalue().encode("utf-8")
    final = folder / f"prices__nse__{last.isoformat()}.csv"
    if final.exists() and final.read_bytes() == data:
        return "unchanged"
    tmp = folder / (final.name + ".part")
    tmp.write_bytes(data)
    os.replace(tmp, final)
    for old in folder.glob("prices__nse__*.csv"):
        if old != final:
            old.unlink()
    return "written"


def update_prices(session, client: NseClient, cfg: dict, local_dir: str, *, now: datetime,
                  symbols: list[str] | None = None, days: int | None = None) -> list[dict]:
    """Bring every tracked company's price file up to the latest published day. One result per symbol:
    {symbol, isin, rows, asof, status: written|unchanged|current|nodata|error, error}. A blocked or odd
    response raises NseError and stops the whole run (it would affect every company)."""
    from sqlalchemy import select
    from ier.db.models import Instrument
    span = int(days if days is not None else cfg["days"])
    end = latest_published(now, cfg)
    q = select(Instrument).where(Instrument.nse_code.is_not(None))
    if symbols:
        q = q.where(Instrument.nse_code.in_([s.strip().upper() for s in symbols if s.strip()]))
    inst = {i.nse_code: i.isin for i in session.scalars(q.order_by(Instrument.nse_code))}
    hist = {s: read_history(local_dir, isin) for s, isin in inst.items()}
    need: dict[str, list[date]] = {}
    for s, h in hist.items():
        start = max(h) + timedelta(days=1) if h else end - timedelta(days=span)
        need[s] = trading_days(start, end)
    fresh: dict[str, dict[date, Row]] = {s: {} for s in inst}
    for day in sorted({d for ds in need.values() for d in ds}):
        wanted = {s for s, ds in need.items() if day in ds}
        text = client.day_file(day)
        if text is None:
            continue
        for sym, row in parse_bhavcopy(text, wanted).items():
            fresh[sym][row[0]] = row
    results = []
    for s in [x.strip().upper() for x in (symbols or []) if x.strip() and x.strip().upper() not in inst]:
        results.append({"symbol": s, "isin": None, "rows": 0, "asof": None, "status": "error", "error": "not in the instruments table"})
    for s, isin in inst.items():
        merged = {**hist[s], **fresh[s]}
        merged = {d: r for d, r in merged.items() if d >= end - timedelta(days=span)}
        rec = {"symbol": s, "isin": isin, "rows": len(merged), "asof": max(merged).isoformat() if merged else None, "status": "", "error": None}
        if not merged:
            rec["status"] = "nodata"
        elif not fresh[s]:
            rec["status"] = "current"
        else:
            try:
                rec["status"] = write_history(local_dir, isin, merged)
            except NseError as exc:
                rec["status"], rec["error"] = "error", str(exc)
        results.append(rec)
    return results
