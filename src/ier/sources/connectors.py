"""One check function per source kind. Each returns a Result and never raises for an expected
condition (missing key, missing file): those are statuses. Network calls go only to the hosts
in ALLOWED_HOSTS, with a timeout, GET only. Keys never appear in a Result or an error text.
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote, urlparse
from xml.etree import ElementTree

from sqlalchemy import select

CONNECTED, NEEDS_KEY, WAITING, STALE, ERROR = "connected", "needs_key", "waiting_for_file", "stale", "error"
STATUSES = (CONNECTED, NEEDS_KEY, WAITING, STALE, ERROR, "not_checked")
ALLOWED_HOSTS = ("api.frankfurter.dev", "api.stlouisfed.org", "news.google.com")
FEED_PREFIXES = ("prices__", "news__", "social__")
ROOT = Path(__file__).resolve().parents[3]
_DATE_FORMATS = ("%Y-%m-%d", "%d-%b-%Y", "%d-%m-%Y", "%d/%m/%Y", "%d %b %Y", "%b %d, %Y")


@dataclass
class Result:
    status: str
    detail: str
    count: int | None = None
    latest: dict | None = None
    points: list[tuple[str, date, float]] = field(default_factory=list)   # (series_id, day, value)


@dataclass
class Ctx:
    session: object
    env: dict
    now: datetime
    local_dir: str = ""
    http: object = None            # object with .get(url, params=None) -> resp(status_code, text, json())
    base_dir: Path = ROOT


class HostNotAllowed(RuntimeError):
    pass


def http_get(ctx: Ctx, url: str, params: dict | None = None):
    host = urlparse(url).hostname or ""
    if urlparse(url).scheme != "https" or host not in ALLOWED_HOSTS:
        raise HostNotAllowed(f"refusing to call {host!r}")
    return ctx.http.get(url, params=params)


def default_http():
    import httpx
    return httpx.Client(timeout=15.0, follow_redirects=False,
                        headers={"User-Agent": "india-equity-research/0.1 (personal research)"})


def _days_old(ts: datetime, now: datetime) -> int:
    return max(0, (now - ts).days)


def _parse_day(text: str) -> date | None:
    text = (text or "").strip().strip('"')
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _num(text: str) -> float | None:
    t = (text or "").strip().strip('"').replace(",", "")
    try:
        v = float(t)
    except ValueError:
        return None
    return v if v == v and abs(v) != float("inf") else None


# --- kinds -----------------------------------------------------------------------------

def check_kite(src: dict, ctx: Ctx) -> Result:
    from ier.tools import zerodha
    cfg = zerodha.load_config()
    try:
        zerodha.load_token(zerodha.token_path(cfg), ctx.now)
    except zerodha.KiteSessionExpired as exc:
        return Result(NEEDS_KEY, str(exc))
    return Result(CONNECTED, "Zerodha session is valid (prices are fetched with: python -m ier.runtime kite-prices)")


def _folder_files(src: dict, ctx: Ctx) -> list[Path]:
    from ier.tools.local_filings import ISIN_RE
    root = Path(ctx.local_dir).expanduser()
    kinds, match = list(src.get("folder_kinds") or []), src.get("match")
    out = []
    for d in sorted(root.iterdir()) if root.is_dir() else []:
        if d.is_symlink() or not d.is_dir() or not ISIN_RE.fullmatch(d.name):
            continue
        for f in sorted(d.iterdir()):
            if f.is_symlink() or not f.is_file():
                continue
            name = f.name.lower()
            if kinds == ["*"]:
                ok = not name.startswith(FEED_PREFIXES)
            else:
                ok = any(name.startswith(k + "__") for k in kinds)
            if ok and (not match or match in name):
                out.append(f)
    return out


def check_local_files(src: dict, ctx: Ctx) -> Result:
    if not ctx.local_dir:
        return Result(ERROR, "local_dir is not set in src/ier/tools/config/exchange_filings.yaml")
    files = _folder_files(src, ctx)
    kinds = ", ".join(src.get("folder_kinds") or [])
    if not files:
        return Result(WAITING, f"no files yet: drop <ISIN>/<kind>__<name>__<date>.* ({kinds}) into {ctx.local_dir}", 0)
    newest = max(datetime.fromtimestamp(f.stat().st_mtime, timezone.utc) for f in files)
    age = _days_old(newest, ctx.now)
    latest = {"files": len(files), "newest_age_days": age}
    if age > int(src.get("stale_days", 30)):
        return Result(STALE, f"{len(files)} file(s), newest is {age} days old", len(files), latest)
    return Result(CONNECTED, f"{len(files)} file(s), newest {age} day(s) old", len(files), latest)


def _read_series_csv(path: Path) -> list[tuple[date, float]]:
    with open(path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    if len(rows) < 2:
        return []
    head = [h.strip().lower() for h in rows[0]]
    col = next((i for i, h in enumerate(head) if h in ("close", "close price", "closing index value")), None)
    if col is None:
        col = next((i for i, h in enumerate(head) if h in ("value", "net", "net value", "close")), len(head) - 1)
    out = []
    for r in rows[1:]:
        if len(r) <= col:
            continue
        d, v = _parse_day(r[0]), _num(r[col])
        if d is not None and v is not None:
            out.append((d, v))
    return out


def check_series_csv(src: dict, ctx: Ctx) -> Result:
    folder = Path(src["folder"])
    folder = folder if folder.is_absolute() else ctx.base_dir / folder
    files = sorted(p for p in folder.glob("*.csv") if p.is_file() and not p.is_symlink()) if folder.is_dir() else []
    if not files:
        return Result(WAITING, f"no CSV yet: save an export (Date,Close) into {src['folder']}/", 0)
    series, points, rows = src.get("series"), [], 0
    newest_day = None
    for p in files:
        try:
            data = _read_series_csv(p)
        except (OSError, csv.Error, UnicodeDecodeError):
            return Result(ERROR, f"could not read {p.name}")
        rows += len(data)
        for d, v in data:
            newest_day = d if newest_day is None or d > newest_day else newest_day
            if series:
                points.append((series, d, v))
    if rows == 0:
        return Result(ERROR, "CSV has no usable rows: expected a Date column and a Close (or Value) column")
    age = (ctx.now.date() - newest_day).days if newest_day else 0
    latest = {"rows": rows, "newest_day": newest_day.isoformat(), "newest_age_days": age}
    if age > int(src.get("stale_days", 30)):
        return Result(STALE, f"{rows} rows, newest is {age} days old ({newest_day})", rows, latest, points)
    return Result(CONNECTED, f"{rows} rows, newest {newest_day}", rows, latest, points)


def check_frankfurter(src: dict, ctx: Ctx) -> Result:
    start = (ctx.now.date() - timedelta(days=int(src.get("days", 120)))).isoformat()
    base, sym = src["base"], src["symbol"]
    try:
        r = http_get(ctx, f"https://api.frankfurter.dev/v1/{start}..", {"base": base, "symbols": sym})
    except HostNotAllowed as exc:
        return Result(ERROR, str(exc))
    except Exception as exc:  # noqa: BLE001
        return Result(ERROR, f"could not reach api.frankfurter.dev ({type(exc).__name__})")
    if r.status_code != 200:
        return Result(ERROR, f"api.frankfurter.dev answered HTTP {r.status_code}")
    rates = (r.json() or {}).get("rates") or {}
    points = []
    for day, row in rates.items():
        d, v = _parse_day(day), (row or {}).get(sym)
        if d is not None and isinstance(v, (int, float)):
            points.append((src["series"], d, float(v)))
    if not points:
        return Result(ERROR, "no rates in the response")
    points.sort(key=lambda p: p[1])
    last = points[-1]
    return Result(CONNECTED, f"{base}/{sym} {last[2]:.4f} on {last[1]}", len(points),
                  {"series": src["series"], "day": last[1].isoformat(), "value": last[2]}, points)


def check_fred(src: dict, ctx: Ctx) -> Result:
    key = (ctx.env.get(src.get("key_env", "")) or "").strip()
    if not key:
        return Result(NEEDS_KEY, f"set {src.get('key_env')} (free key from fred.stlouisfed.org)")
    points, latest, problems = [], {}, []
    for name, fred_id in (src.get("series") or {}).items():
        try:
            r = http_get(ctx, "https://api.stlouisfed.org/fred/series/observations",
                         {"series_id": fred_id, "api_key": key, "file_type": "json",
                          "sort_order": "desc", "limit": 60})
        except Exception as exc:  # noqa: BLE001 - never include the exception text: it can carry the key
            problems.append(f"{fred_id}: {type(exc).__name__}")
            continue
        if r.status_code in (400, 401, 403):
            return Result(NEEDS_KEY, f"FRED rejected the key (HTTP {r.status_code}): check {src.get('key_env')}")
        if r.status_code != 200:
            problems.append(f"{fred_id}: HTTP {r.status_code}")
            continue
        got = []
        for o in (r.json() or {}).get("observations") or []:
            d, v = _parse_day(o.get("date", "")), _num(o.get("value", ""))
            if d is not None and v is not None:
                got.append((name, d, v))
        if got:
            got.sort(key=lambda p: p[1])
            points += got
            latest[name] = {"day": got[-1][1].isoformat(), "value": got[-1][2]}
    if not points:
        return Result(ERROR, "FRED returned no data" + (f" ({'; '.join(problems)})" if problems else ""))
    detail = f"{len(latest)} series, {len(points)} points"
    if problems:
        detail += f"; problems: {'; '.join(problems)}"
    return Result(CONNECTED if not problems else STALE, detail, len(points), latest, points)


def _company_rows(ctx: Ctx):
    from ier.db.models import Agent, Instrument
    return ctx.session.execute(
        select(Instrument.isin, Instrument.name).join(Agent, Agent.scope_instrument_id == Instrument.id)
        .where(Agent.role == "company").order_by(Instrument.name)).all()


def _parse_rss(text: str, limit: int) -> list[tuple[str, str, str]]:
    if re.search(r"<!(DOCTYPE|ENTITY)", text, re.I):
        raise ValueError("refusing XML with a DOCTYPE/ENTITY")
    root = ElementTree.fromstring(text)
    items = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        pub = (it.findtext("pubDate") or "").strip()
        try:
            day = parsedate_to_datetime(pub).date().isoformat()
        except (TypeError, ValueError):
            day = ""
        source = (it.findtext("source") or "").strip()
        if title:
            items.append((day, title, source))
        if len(items) >= limit:
            break
    return items


def check_rss_news(src: dict, ctx: Ctx) -> Result:
    if not ctx.local_dir:
        return Result(ERROR, "local_dir is not set in src/ier/tools/config/exchange_filings.yaml")
    companies = _company_rows(ctx)
    if not companies:
        return Result(WAITING, "no company agents yet: run the seed command first", 0)
    written, total, problems = 0, 0, []
    for isin, name in companies:
        q = quote(f'"{name}" share when:7d')
        try:
            r = http_get(ctx, f"https://news.google.com/rss/search?q={q}&hl=en-IN&gl=IN&ceid=IN:en")
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{name}: {type(exc).__name__}")
            continue
        if r.status_code != 200:
            problems.append(f"{name}: HTTP {r.status_code}")
            continue
        try:
            items = _parse_rss(r.text, int(src.get("max_items", 15)))
        except (ElementTree.ParseError, ValueError):
            problems.append(f"{name}: unreadable feed")
            continue
        if not items:
            continue
        total += len(items)
        body = ("Source: Google News RSS (headlines only, unverified third-party reporting)\n"
                f"Company: {name}\n\n" + "\n".join(f"- {d} | {t}" + (f" ({s})" if s else "") for d, t, s in items) + "\n")
        folder = Path(ctx.local_dir).expanduser() / isin
        if not folder.is_dir():
            folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"news__rss-google__{ctx.now.date().isoformat()}.txt"
        old = [p for p in folder.glob("news__rss-google__*.txt") if p != path]
        if path.exists() and path.read_text(encoding="utf-8") == body:
            continue
        tmp = path.with_name(path.name + ".part")
        tmp.write_text(body, encoding="utf-8")
        tmp.replace(path)
        for p in old:
            p.unlink(missing_ok=True)
        written += 1
    if total == 0:
        return Result(ERROR if problems else WAITING,
                      "no headlines" + (f" ({'; '.join(problems[:3])})" if problems else ""), 0)
    detail = f"{total} headlines for {len(companies)} companies, {written} file(s) updated"
    if problems:
        detail += f"; problems: {'; '.join(problems[:3])}"
    return Result(CONNECTED if not problems else STALE, detail, total, {"files_written": written})


def check_derived(src: dict, ctx: Ctx) -> Result:
    from ier.db.models import Instrument, Scorecard
    rows = ctx.session.execute(
        select(Instrument.sector, Instrument.name, Scorecard.short_term_score, Scorecard.long_term_score,
               Scorecard.run_date).join(Scorecard, Scorecard.instrument_id == Instrument.id)
        .order_by(Scorecard.run_date.desc(), Scorecard.id.desc())).all()
    seen, ranked = set(), []
    for sector, name, short, long_, run_date in rows:
        if name in seen:
            continue
        seen.add(name)
        ranked.append({"sector": sector, "name": name, "short": short, "long": long_})
    ranked.sort(key=lambda r: (r["short"] is None, -(r["short"] or 0)))
    if len(ranked) < 2:
        return Result(WAITING, "needs scorecards for at least 2 companies", len(ranked))
    return Result(CONNECTED, f"ranked {len(ranked)} companies by short-term score", len(ranked), {"ranked": ranked[:10]})


CHECKS = {"kite_prices": check_kite, "local_files": check_local_files, "series_csv": check_series_csv,
          "frankfurter": check_frankfurter, "fred": check_fred, "rss_news": check_rss_news,
          "derived": check_derived}


def run_check(src: dict, ctx: Ctx) -> Result:
    if ctx.http is None and src["kind"] in ("frankfurter", "fred", "rss_news"):
        ctx.http = default_http()
    try:
        return CHECKS[src["kind"]](src, ctx)
    except Exception as exc:  # noqa: BLE001 - one source must never break the others
        return Result(ERROR, f"check failed: {type(exc).__name__}")
