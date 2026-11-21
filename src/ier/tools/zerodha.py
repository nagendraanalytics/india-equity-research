"""Read-only Zerodha Kite Connect client: daily price history for the research pipeline.

Scope, on purpose: it lists NSE instruments and downloads daily candles. It has no code path for
trading, positions, holdings or funds, and the allow-list below refuses every other URL. Its
output is a price CSV in the company's folder (prices__zerodha__<date>.csv), which the existing
technicals agent reads like any other price file, so the audit trail and the change detection
stay the same.

Personal research use only: Kite data must not be shown to or redistributed to other people.
Secrets: IER_KITE_API_KEY and IER_KITE_API_SECRET come from the environment. The day's access
token is kept in a local, git-ignored file and is never printed or logged.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import os
import re
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import yaml

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[3]
CONFIG_PATH = ROOT / "config" / "zerodha.yaml"
API = "https://api.kite.trade"
LOGIN = "https://kite.zerodha.com/connect/login"
IST = timezone(timedelta(hours=5, minutes=30))
CLOSE_TIME = (15, 45)        # after this IST time the day's candle is final
SESSION_EXPIRES_AT = (6, 0)  # Kite access tokens expire at 6 AM IST

# The only paths this client will ever request (plus the one-off session exchange below).
_ALLOWED = (
    re.compile(r"/instruments/(NSE|BSE)"),
    re.compile(r"/instruments/historical/\d{1,12}/day"),
)
_TOKEN_RE = re.compile(r"[A-Za-z0-9]{8,64}")
_SYMBOL_RE = re.compile(r"[A-Z0-9&\-]{1,20}")

DEFAULTS = {
    "symbols": ["INFY", "TCS", "HCLTECH", "TECHM", "WIPRO"],
    "days": 400,                       # calendar days of history (the 200-day average needs ~290)
    "token_file": "data/.kite_token.json",
    "min_gap_seconds": 0.4,            # Kite allows about 3 historical requests a second
}


class KiteError(RuntimeError):
    """A Kite request failed or was refused."""


class KiteSessionExpired(KiteError):
    """No valid access token: run `python -m ier.runtime kite-login`."""


def load_config(path: Path = CONFIG_PATH) -> dict:
    cfg = dict(DEFAULTS)
    if path.exists():
        with open(path) as f:
            cfg.update(yaml.safe_load(f) or {})
    return cfg


def token_path(cfg: dict) -> Path:
    p = Path(cfg["token_file"]).expanduser()
    return p if p.is_absolute() else ROOT / p


def clean_symbols(symbols) -> list[str]:
    out = []
    for s in symbols:
        s = str(s).strip().upper()
        if not _SYMBOL_RE.fullmatch(s):
            raise KiteError(f"not a valid NSE symbol: {s!r}")
        if s not in out:
            out.append(s)
    return out


# ---- session -------------------------------------------------------------------------------

def next_expiry(now: datetime) -> datetime:
    """The next 6 AM IST after `now`: when a token issued at `now` stops working."""
    now = now.astimezone(IST)
    cand = now.replace(hour=SESSION_EXPIRES_AT[0], minute=SESSION_EXPIRES_AT[1], second=0, microsecond=0)
    return cand if now < cand else cand + timedelta(days=1)


def save_token(path: Path, api_key: str, access_token: str, now: datetime) -> datetime:
    expires = next_expiry(now)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    tmp.write_text(json.dumps({"api_key": api_key, "access_token": access_token,
                               "expires_at": expires.isoformat()}), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:     # e.g. Windows: best effort only
        pass
    os.replace(tmp, path)
    return expires


def load_token(path: Path, now: datetime) -> tuple[str, str]:
    """(api_key, access_token) saved by `kite-login`, or KiteSessionExpired."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        expires = datetime.fromisoformat(data["expires_at"])
        key, token = data["api_key"], data["access_token"]
    except (OSError, ValueError, KeyError):
        raise KiteSessionExpired("no saved Zerodha session: run `python -m ier.runtime kite-login`") from None
    if now >= expires:
        raise KiteSessionExpired("the saved Zerodha session has expired (they end at 6 AM): "
                                 "run `python -m ier.runtime kite-login`")
    return key, token


def login_url(api_key: str) -> str:
    return f"{LOGIN}?v=3&api_key={api_key}"


def request_token_from(text: str) -> str:
    """The request token from the pasted redirect URL, or the bare token itself."""
    text = (text or "").strip().strip("'\"")
    if "request_token=" in text:
        vals = parse_qs(urlparse(text).query).get("request_token") or []
        text = vals[0] if vals else ""
    if not _TOKEN_RE.fullmatch(text):
        raise KiteError("that does not look like a request token: paste the whole address "
                        "you were redirected to, or just the request_token value")
    return text


def _check(resp) -> dict:
    """The parsed JSON body of a Kite response, or a KiteError (never includes credentials)."""
    try:
        body = resp.json()
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}
    if resp.status_code == 200 and body.get("status") != "error":
        return body
    etype = str(body.get("error_type") or "")
    msg = str(body.get("message") or f"HTTP {resp.status_code}")[:200]
    if etype == "TokenException":
        raise KiteSessionExpired(f"Zerodha session not accepted ({msg}): run `kite-login` again")
    if resp.status_code == 403:
        raise KiteError(f"Zerodha refused the request ({msg}): historical data needs the paid "
                        "Kite Connect app, not the free Personal one")
    raise KiteError(f"Zerodha error: {msg}")


def exchange_request_token(api_key: str, api_secret: str, request_token: str, *, http=None) -> str:
    """Trade the one-off request token for the day's access token."""
    http = http or httpx.Client(timeout=30)
    checksum = hashlib.sha256((api_key + request_token + api_secret).encode()).hexdigest()
    resp = http.post(f"{API}/session/token", headers={"X-Kite-Version": "3"},
                     data={"api_key": api_key, "request_token": request_token, "checksum": checksum})
    token = (_check(resp).get("data") or {}).get("access_token")
    if not token:
        raise KiteError("Zerodha did not return an access token")
    return token


def login_flow(env, cfg: dict, *, input_fn=input, print_fn=print, http=None,
               now: datetime | None = None) -> int:
    """Interactive login: show the link, read the pasted redirect, save the day's token."""
    key, secret = env.get("IER_KITE_API_KEY", "").strip(), env.get("IER_KITE_API_SECRET", "").strip()
    if not key or not secret:
        print_fn("Set IER_KITE_API_KEY and IER_KITE_API_SECRET first (no quotes), then run this again.")
        return 1
    now = now or datetime.now(timezone.utc)
    print_fn("1. Open this address in your browser and log in to Zerodha:")
    print_fn("   " + login_url(key))
    print_fn("2. After the login the browser is redirected to your app's redirect address. "
             "Copy the whole address from the browser's address bar.")
    try:
        req = request_token_from(input_fn("3. Paste it here: "))
        token = exchange_request_token(key, secret, req, http=http)
    except KiteError as exc:
        print_fn(f"error: {exc}")
        return 1
    expires = save_token(token_path(cfg), key, token, now)
    print_fn(f"Logged in. The session works until {expires:%d %b %Y %H:%M} IST.")
    return 0


# ---- data ------------------------------------------------------------------------------------

class KiteClient:
    """Read-only: instruments and daily candles. `http` is any object with get/post like httpx."""

    def __init__(self, api_key: str, access_token: str, *, http=None, min_gap: float = 0.4,
                 sleep=time.sleep):
        self._headers = {"X-Kite-Version": "3", "Authorization": f"token {api_key}:{access_token}"}
        self._http = http or httpx.Client(timeout=30)
        self._min_gap, self._sleep = min_gap, sleep
        self._last = 0.0
        self._instruments: dict[str, list[dict]] = {}

    def _get(self, path: str, params: dict | None = None):
        if not any(p.fullmatch(path) for p in _ALLOWED):
            raise KiteError(f"refused: {path} is not an allowed read-only path")
        wait = self._min_gap - (time.monotonic() - self._last)
        if wait > 0:
            self._sleep(wait)
        self._last = time.monotonic()
        return self._http.get(API + path, headers=self._headers, params=params)

    def instruments(self, exchange: str = "NSE") -> list[dict]:
        if exchange not in self._instruments:
            resp = self._get(f"/instruments/{exchange}")
            if resp.status_code != 200:
                _check(resp)
            self._instruments[exchange] = list(csv.DictReader(io.StringIO(resp.text)))
        return self._instruments[exchange]

    def historical(self, instrument_token: int, start: date, end: date) -> list[tuple]:
        """[(date, open, high, low, close, volume)] oldest first."""
        resp = self._get(f"/instruments/historical/{int(instrument_token)}/day",
                         {"from": f"{start} 00:00:00", "to": f"{end} 23:59:59"})
        candles = (_check(resp).get("data") or {}).get("candles") or []
        out = []
        for c in candles:
            try:
                out.append((date.fromisoformat(str(c[0])[:10]), float(c[1]), float(c[2]),
                            float(c[3]), float(c[4]), int(c[5])))
            except (IndexError, TypeError, ValueError):
                continue
        return sorted(out)


def resolve_tokens(rows: list[dict], symbols: list[str]) -> dict[str, int]:
    """symbol -> instrument_token for the NSE cash-market equities among `symbols`."""
    wanted = set(symbols)
    out = {}
    for r in rows:
        sym = (r.get("tradingsymbol") or "").strip()
        if (sym in wanted and r.get("exchange") == "NSE" and r.get("segment") == "NSE"
                and r.get("instrument_type") == "EQ"):
            try:
                out[sym] = int(r["instrument_token"])
            except (KeyError, ValueError):
                continue
    return out


def settle_candles(candles: list[tuple], now: datetime, include_today: bool) -> list[tuple]:
    """Drop today's unfinished candle (before 15:45 IST) unless `include_today`."""
    now = now.astimezone(IST)
    if (not include_today and candles and candles[-1][0] == now.date()
            and (now.hour, now.minute) < CLOSE_TIME):
        return candles[:-1]
    return candles


def candles_csv(candles: list[tuple]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["Date", "Open", "High", "Low", "Close", "Volume"])
    for d, o, h, lo, c, v in candles:
        w.writerow([d.isoformat(), f"{o:.2f}", f"{h:.2f}", f"{lo:.2f}", f"{c:.2f}", v])
    return buf.getvalue()


def write_price_file(local_dir: str, isin: str, asof: date, text: str) -> str:
    """Write prices__zerodha__<asof>.csv into <local_dir>/<isin>/ atomically; 'written' or
    'unchanged' (identical content: nothing is touched, so the watcher is not woken)."""
    from ier.tools.local_filings import ISIN_RE
    if not ISIN_RE.fullmatch(isin or ""):
        raise KiteError(f"not an ISIN: {isin!r}")
    root = Path(local_dir).expanduser().resolve()
    if not root.is_dir():
        raise KiteError(f"local_dir {str(root)!r} is not a folder")
    folder = root / isin
    folder.mkdir(exist_ok=True)
    if folder.is_symlink() or not folder.resolve().is_relative_to(root):
        raise KiteError(f"refusing to write outside local_dir: {folder}")
    final = folder / f"prices__zerodha__{asof.isoformat()}.csv"
    data = text.encode("utf-8")
    if final.exists() and final.read_bytes() == data:
        return "unchanged"
    tmp = folder / (final.name + ".part")        # .part is not a feed or filing extension
    tmp.write_bytes(data)
    os.replace(tmp, final)
    for old in folder.glob("prices__zerodha__*.csv"):   # keep one file per company
        if old != final:
            old.unlink()
    return "written"


def fetch_prices(session, client: KiteClient, cfg: dict, local_dir: str, *, now: datetime,
                 symbols: list[str] | None = None, include_today: bool = False,
                 days: int | None = None) -> list[dict]:
    """Download daily candles for each symbol and write its price file. One result per symbol:
    {symbol, isin, rows, asof, status: written|unchanged|error, error}. A rejected session
    aborts the whole run (KiteSessionExpired); any other per-symbol problem is reported."""
    from sqlalchemy import select
    from ier.db.models import Instrument
    syms = clean_symbols(symbols if symbols is not None else cfg["symbols"])
    isins = {r.nse_code: r.isin for r in session.execute(
        select(Instrument).where(Instrument.nse_code.in_(syms))).scalars()}
    tokens = resolve_tokens(client.instruments("NSE"), syms)
    start = now.astimezone(IST).date() - timedelta(days=int(days or cfg["days"]))
    end = now.astimezone(IST).date()
    results = []
    for sym in syms:
        res = {"symbol": sym, "isin": isins.get(sym), "rows": 0, "asof": None,
               "status": "error", "error": None}
        results.append(res)
        if sym not in isins:
            res["error"] = "not in the instruments table (load the classification first)"
            continue
        if sym not in tokens:
            res["error"] = "not found in Zerodha's NSE instrument list"
            continue
        try:
            candles = settle_candles(client.historical(tokens[sym], start, end), now, include_today)
            if len(candles) < 2:
                res["error"] = "Zerodha returned no usable candles"
                continue
            res["rows"], res["asof"] = len(candles), candles[-1][0].isoformat()
            res["status"] = write_price_file(local_dir, isins[sym], candles[-1][0], candles_csv(candles))
        except KiteSessionExpired:
            raise
        except KiteError as exc:
            res["error"] = str(exc)
    return results
