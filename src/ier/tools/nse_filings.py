"""NSE quarterly results and annual reports, downloaded into each company's folder.

Writes <local_dir>/<ISIN>/<filing_type>__<period>__<YYYY-MM-DD>.pdf, the layout local_filings already
reads, so the company graph and the `watch` command need no change. Read-only against NSE; the only hosts
ever requested are those in config/nse_filings.yaml `allowed_hosts`, and redirects are refused.

UNVERIFIED, like the other exchange connectors: NSE's terms of use, the endpoint paths and the JSON field
names are not confirmed (see config/nse_filings.yaml and docs/PLACEHOLDERS.md). NSE may block server
addresses; the command then reports "blocked" and saves nothing.
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx
import yaml

ISIN_RE = re.compile(r"[A-Z0-9]{12}")   # same rule as local_filings; kept here so this module needs no database layer

ROOT = Path(__file__).resolve().parents[3]
CONFIG_PATH = ROOT / "config" / "nse_filings.yaml"
DEFAULTS = {
    "results_years": 2,
    "annual_years": 3,
    "min_gap_seconds": 1.0,
    "max_mb_results": 20,
    "max_mb_annual": 60,
    "timeout_seconds": 30,
    "user_agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "allowed_hosts": ["www.nseindia.com", "nsearchives.nseindia.com", "archives.nseindia.com"],
    "endpoints": {
        "results": "/api/corporates-financial-results?index=equities&symbol={symbol}&period=Quarterly",
        "annual": "/api/annual-reports?index=equities&symbol={symbol}",
        "announcements": "/api/corporate-announcements?index=equities&symbol={symbol}&from_date={from}&to_date={to}",
    },
}


class NseError(Exception):
    pass


class NseBlocked(NseError):
    """NSE refused the session (HTTP 401/403, or a web page instead of data). Affects every company."""


def load_config(path: Path = CONFIG_PATH) -> dict:
    cfg = {k: (dict(v) if isinstance(v, dict) else list(v) if isinstance(v, list) else v) for k, v in DEFAULTS.items()}
    if path.exists():
        cfg.update(yaml.safe_load(path.read_text(encoding="utf-8")) or {})
    return cfg


def quarter_label(period_end: date) -> str:
    """Indian financial year starts 1 April: 31 Mar 2026 -> Q4FY26, 30 Jun 2026 -> Q1FY27."""
    fy_end_year = period_end.year + (1 if period_end.month >= 4 else 0)
    quarter = ((period_end.month - 4) % 12) // 3 + 1
    return f"Q{quarter}FY{fy_end_year % 100:02d}"


def fy_label(year_end: date) -> str:
    return f"FY{year_end.year + (1 if year_end.month >= 4 else 0)}"


def filing_name(kind: str, period: str, published: date) -> str:
    return f"{kind}__{period}__{published.isoformat()}.pdf"


def host_allowed(url: str, cfg: dict) -> bool:
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return parts.scheme == "https" and (parts.hostname or "") in set(cfg["allowed_hosts"])


def looks_like_pdf(body: bytes) -> bool:
    return body[:5] == b"%PDF-"


BASE = "https://www.nseindia.com"
RETRIES = 2        # extra tries after a 5xx or a timeout


class NseFilingsClient:
    """Cookie session plus JSON calls and PDF downloads. Redirects are refused, only allow-listed hosts
    are requested, and a refusal by NSE (403, or a web page instead of data) raises NseBlocked."""

    def __init__(self, cfg: dict, *, transport: httpx.BaseTransport | None = None, sleep=time.sleep):
        self._cfg = cfg
        self._http = httpx.Client(
            transport=transport, timeout=float(cfg["timeout_seconds"]), follow_redirects=False,
            headers={"User-Agent": cfg["user_agent"], "Accept": "application/json, text/plain, */*",
                     "Accept-Language": "en-US,en;q=0.9", "Referer": BASE + "/"})
        self._gap, self._sleep, self._last, self._ready = float(cfg["min_gap_seconds"]), sleep, 0.0, False

    def _pause(self) -> None:
        wait = self._gap - (time.monotonic() - self._last)
        if wait > 0:
            self._sleep(wait)
        self._last = time.monotonic()

    def _get(self, url: str) -> httpx.Response:
        last = "no response"
        for _ in range(RETRIES + 1):
            self._pause()
            try:
                r = self._http.get(url)
            except httpx.HTTPError as exc:
                last = f"could not reach NSE: {exc.__class__.__name__}"
                continue
            if 300 <= r.status_code < 400:
                raise NseError(f"redirect refused for {url}: only the listed NSE hosts are allowed")
            if r.status_code in (401, 403):
                raise NseBlocked(f"NSE refused the request (HTTP {r.status_code}); the server address may be "
                                 "blocked. Run from a home connection or place the files by hand.")
            if r.status_code >= 500:
                last = f"NSE returned HTTP {r.status_code}"
                continue
            return r
        raise NseError(last)

    def get_json(self, path_and_query: str):
        if not self._ready:
            self._get(BASE + "/")
            self._ready = True
        r = self._get(BASE + path_and_query)
        if r.status_code != 200:
            raise NseError(f"NSE returned HTTP {r.status_code} for {path_and_query.split('?')[0]}")
        try:
            return json.loads(r.text)
        except ValueError:
            raise NseBlocked("NSE returned a page instead of data (session refused?)") from None

    def download(self, url: str, max_bytes: int) -> bytes | None:
        """The file's bytes, or None when it is off-list, too large or not a PDF."""
        if not host_allowed(url, self._cfg):
            return None
        r = self._get(url)
        if r.status_code != 200:
            return None
        body = r.content
        if len(body) > max_bytes or not looks_like_pdf(body):
            return None
        return body


# --- listing -> files ----------------------------------------------------------------------------

# UNVERIFIED field names, tried in order. Confirm against the live response and trim.
URL_KEYS = ("attachment", "attchmntFile", "fileName", "xbrl", "pdf", "link")
PUBLISHED_KEYS = ("broadcastDate", "broadCastDate", "an_dt", "disclosureDate", "filingDate", "date")
PERIOD_END_KEYS = ("toDate", "qe_Date", "to_date", "periodEnd")
FY_END_YEAR_KEYS = ("toYr", "toYear")
DATE_FORMATS = ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y", "%Y-%m-%d", "%d-%m-%Y", "%Y-%m-%d %H:%M:%S", "%d-%b-%Y %H:%M")
KIND_FILE = {"results": "results", "annual": "annual_report"}


def _date(value) -> date | None:
    text = str(value or "").strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _first(item: dict, keys) -> object:
    for k in keys:
        if item.get(k):
            return item[k]
    return None


def parse_items(kind: str, raw) -> list[dict]:
    """Normalise NSE's listing to [{period, published, url}], dropping items without a url, a filing
    date or a period. `kind` is "results" or "annual"."""
    rows = raw.get("data", []) if isinstance(raw, dict) else raw
    out = []
    for it in rows if isinstance(rows, list) else []:
        if not isinstance(it, dict):
            continue
        url, published = _first(it, URL_KEYS), _date(_first(it, PUBLISHED_KEYS))
        if not isinstance(url, str) or published is None:
            continue
        if kind == "results":
            end = _date(_first(it, PERIOD_END_KEYS))
            period = quarter_label(end) if end else None
        else:
            year = str(_first(it, FY_END_YEAR_KEYS) or "")
            period = f"FY{year}" if re.fullmatch(r"\d{4}", year) else None
        if period:
            out.append({"period": period, "published": published, "url": url})
    return out


def _folder(local_dir: str, isin: str) -> Path:
    if not ISIN_RE.fullmatch(isin or ""):
        raise NseError(f"refusing a folder for the invalid ISIN {isin!r}")
    root = Path(local_dir).expanduser().resolve()
    folder = root / isin
    if folder.is_symlink() or (folder.exists() and not folder.is_dir()):
        raise NseError(f"{folder} is not a plain folder")
    if folder.resolve().parent != root:
        raise NseError(f"{folder} is outside {root}")
    return folder          # not created here: only a real write makes the folder


def _save(folder: Path, name: str, body: bytes) -> None:
    """Atomic: write a temp file, then rename. No temp file survives a failure."""
    folder.mkdir(parents=True, exist_ok=True)
    tmp = folder / (name + ".tmp")
    try:
        tmp.write_bytes(body)
        os.replace(tmp, folder / name)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _one(client, cfg: dict, local_dir: str, symbol: str, isin: str, kind: str, now: datetime) -> dict:
    rec = {"symbol": symbol, "isin": isin, "kind": kind, "status": "nodata", "files": 0, "error": None}
    path = cfg["endpoints"][kind].format(symbol=quote(symbol), **{"from": "", "to": ""})
    items = parse_items(kind, client.get_json(path))
    years = int(cfg["results_years"] if kind == "results" else cfg["annual_years"])
    cutoff = now.date() - timedelta(days=366 * years)
    items = [i for i in items if i["published"] >= cutoff]
    if not items:
        return rec
    limit = int(cfg["max_mb_results"] if kind == "results" else cfg["max_mb_annual"]) * 1024 * 1024
    folder, written, existing, skipped = None, 0, 0, 0
    for it in sorted(items, key=lambda i: i["published"]):
        folder = folder or _folder(local_dir, isin)
        if folder.is_dir() and any(folder.glob(f"{KIND_FILE[kind]}__{it['period']}__*.pdf")):
            existing += 1
            continue
        body = client.download(it["url"], limit) if host_allowed(it["url"], cfg) else None
        if body is None:
            skipped += 1
            continue
        _save(folder, filing_name(KIND_FILE[kind], it["period"], it["published"]), body)
        written += 1
    rec["files"] = written
    if written:
        rec["status"] = "written"
    elif existing:
        rec["status"] = "unchanged"
    else:
        rec["status"], rec["error"] = "error", "attachment not downloadable (off-list host, too large or not a PDF)"
    return rec


def update_filings_for(inst: dict[str, str], client, cfg: dict, local_dir: str, *, now: datetime,
                       kinds: tuple[str, ...] = ("results", "annual"), unknown: tuple[str, ...] = ()) -> list[dict]:
    """The download loop for explicit {NSE symbol: ISIN} pairs; no database. One result per symbol and
    kind: {symbol, isin, kind, status: written|unchanged|nodata|blocked|error, files, error}. A NseBlocked
    affects every company, so it ends the run: the rest come back `blocked`. `unknown` symbols are
    reported as errors."""
    results: list[dict] = []
    for s in unknown:
        for kind in kinds:
            results.append({"symbol": s, "isin": None, "kind": kind, "status": "error", "files": 0,
                            "error": "not in the instruments table"})
    blocked = None
    for symbol, isin in inst.items():
        for kind in kinds:
            if blocked:
                results.append({"symbol": symbol, "isin": isin, "kind": kind, "status": "blocked", "files": 0, "error": blocked})
                continue
            try:
                results.append(_one(client, cfg, local_dir, symbol, isin, kind, now))
            except NseBlocked as exc:
                blocked = str(exc)
                results.append({"symbol": symbol, "isin": isin, "kind": kind, "status": "blocked", "files": 0, "error": blocked})
            except (NseError, OSError) as exc:
                results.append({"symbol": symbol, "isin": isin, "kind": kind, "status": "error", "files": 0, "error": str(exc)})
    return results


def update_filings(session, client, cfg: dict, local_dir: str, *, now: datetime,
                   symbols: list[str] | None = None, kinds: tuple[str, ...] = ("results", "annual")) -> list[dict]:
    """Download missing quarterly results and annual reports for every company with an NSE code
    (the instruments table decides which). See update_filings_for for the result shape."""
    from sqlalchemy import select

    from ier.db.models import Instrument
    q = select(Instrument).where(Instrument.nse_code.is_not(None))
    wanted = [s.strip().upper() for s in (symbols or []) if s.strip()]
    if wanted:
        q = q.where(Instrument.nse_code.in_(wanted))
    inst = {i.nse_code: i.isin for i in session.scalars(q.order_by(Instrument.nse_code))}
    unknown = tuple(s for s in wanted if s not in inst)
    return update_filings_for(inst, client, cfg, local_dir, now=now, kinds=kinds, unknown=unknown)


# --- standalone mode (python -m ier.tools.nse_filings): for a home connection, no database --------

def _parse_pairs(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        sym, sep, isin = part.partition("=")
        sym, isin = sym.strip().upper(), isin.strip().upper()
        if not sep or not sym or not ISIN_RE.fullmatch(isin):
            raise SystemExit(f"error: {part!r} must look like SYMBOL=ISIN (12 letters/digits), e.g. ITC=INE154A01025")
        out[sym] = isin
    if not out:
        raise SystemExit("error: --isin needs at least one SYMBOL=ISIN pair")
    return out


def main(argv: list[str] | None = None, *, client=None, now: datetime | None = None) -> int:
    """Download filings for explicit SYMBOL=ISIN pairs into --out, without a database. Meant for a home
    connection when the server address is blocked; copy the folder to the server afterwards."""
    import argparse
    p = argparse.ArgumentParser(prog="python -m ier.tools.nse_filings", description=main.__doc__)
    p.add_argument("--isin", required=True, help="comma-separated SYMBOL=ISIN pairs, e.g. ITC=INE154A01025,DABUR=INE016A01026")
    p.add_argument("--out", required=True, help="folder to write <ISIN>/<file>.pdf into (created if missing)")
    p.add_argument("--kind", choices=("results", "annual", "all"), default="all")
    a = p.parse_args(argv)
    pairs = _parse_pairs(a.isin)
    out = Path(a.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    kinds = ("results", "annual") if a.kind == "all" else (a.kind,)
    cfg = load_config()
    results = update_filings_for(pairs, client or NseFilingsClient(cfg), cfg, str(out),
                                 now=now or datetime.now().astimezone(), kinds=kinds)
    for r in results:
        tail = r["error"] or f"{r['files']} new file(s)"
        print(f"{r['symbol']:<12} {r['isin'] or '-':<13} {r['kind']:<8} {r['status']:<9} {tail}")
    return 1 if any(r["status"] in ("blocked", "error") for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
