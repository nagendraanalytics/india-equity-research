"""Price and technicals agent: deterministic indicators from a price CSV. No model call.

Input: prices__*.csv in the company's folder, one row per trading day with a date, a close and
(optionally) a volume column; common NSE/BSE/Yahoo header names are understood. Output: Facts on
the instrument for the rules (price_vs_sma200, price_vs_sma50, momentum_3m, rsi14 in percent /
index points, volume_ratio_20d), from a Tier 2 `price` Source. Research output, not advice.
"""
from __future__ import annotations

import csv
import hashlib
import io
import logging
import re
from datetime import date, datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ier.db.models import Fact, Source
from ier.tools.feeds import PRICES, list_feed_files

log = logging.getLogger(__name__)
PRICE_TIER = 2
DATE_COLS = ("date", "timestamp", "datetime", "trade date", "tradedate", "date1")
CLOSE_COLS = ("close", "close price", "closeprice", "close_price", "adj close", "last price", "ltp")
VOL_COLS = ("volume", "ttl_trd_qnty", "tottrdqty", "total traded quantity", "share volume", "no. of shares")
DATE_FORMATS = ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d-%b-%Y", "%d-%b-%y", "%Y/%m/%d", "%d %b %Y")


def _num(text: str) -> float | None:
    t = re.sub(r"[,\s₹]", "", text or "")
    try:
        v = float(t)
    except ValueError:
        return None
    return v if v == v and abs(v) != float("inf") else None


def _date(text: str) -> date | None:
    t = (text or "").strip()[:20]
    for f in DATE_FORMATS:
        try:
            return datetime.strptime(t, f).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat((text or "").strip().replace("Z", "+00:00")).date()
    except ValueError:
        return None


def parse_prices(text: str) -> list[tuple[date, float, float | None]]:
    """[(date, close, volume|None)] oldest first, deduplicated by date (last row wins)."""
    reader = csv.reader(io.StringIO(text.lstrip("﻿")))
    rows = list(reader)
    if not rows:
        return []
    head = [h.strip().lower() for h in rows[0]]
    def col(names):
        return next((head.index(n) for n in names if n in head), None)
    di, ci, vi = col(DATE_COLS), col(CLOSE_COLS), col(VOL_COLS)
    if di is None or ci is None:
        return []
    out: dict[date, tuple[float, float | None]] = {}
    for r in rows[1:]:
        if len(r) <= max(di, ci):
            continue
        d, c = _date(r[di]), _num(r[ci])
        if d is None or c is None or c <= 0:
            continue
        v = _num(r[vi]) if vi is not None and len(r) > vi else None
        out[d] = (c, v)
    return [(d, c, v) for d, (c, v) in sorted(out.items())]


def _rsi(closes: list[float], n: int = 14) -> float | None:
    if len(closes) < n + 1:
        return None
    ch = [b - a for a, b in zip(closes, closes[1:])]
    gain = sum(max(x, 0) for x in ch[:n]) / n
    loss = sum(max(-x, 0) for x in ch[:n]) / n
    for x in ch[n:]:
        gain = (gain * (n - 1) + max(x, 0)) / n
        loss = (loss * (n - 1) + max(-x, 0)) / n
    return 100.0 if loss == 0 else 100 - 100 / (1 + gain / loss)


def compute_metrics(rows: list[tuple[date, float, float | None]]) -> dict[str, float]:
    """Indicators as of the last row; a metric is omitted when there is not enough history."""
    closes = [c for _, c, _ in rows]
    out: dict[str, float] = {}
    if not closes:
        return out
    last = closes[-1]
    if len(closes) >= 200:
        out["price_vs_sma200"] = (last / (sum(closes[-200:]) / 200) - 1) * 100
    if len(closes) >= 50:
        out["price_vs_sma50"] = (last / (sum(closes[-50:]) / 50) - 1) * 100
    if len(closes) >= 64:
        out["momentum_3m"] = (last / closes[-64] - 1) * 100
    rsi = _rsi(closes)
    if rsi is not None:
        out["rsi14"] = rsi
    vols = [v for _, _, v in rows if v]
    if len(vols) >= 21 and rows[-1][2]:
        out["volume_ratio_20d"] = rows[-1][2] / (sum(vols[-21:-1]) / 20)
    return {k: round(v, 4) for k, v in out.items()}


def extract_technicals(session: Session, instrument_id: int, isin: str, local_dir: str | None) -> dict:
    """Compute indicators from the instrument's price files and save them as Facts.

    Returns {"files": n, "as_of": iso date|None, "metrics": {...}, "saved": n}. Idempotent: a
    metric already saved for the same price date is not duplicated. Flushes, never commits."""
    empty = {"files": 0, "as_of": None, "metrics": {}, "saved": 0, "source_id": None}
    if not local_dir:
        return empty
    paths: list[Path] = list_feed_files(isin, local_dir, PRICES)
    rows: dict[date, tuple[float, float | None]] = {}
    newest: Path | None = None
    for p in paths:
        for d, c, v in parse_prices(p.read_text(encoding="utf-8", errors="replace")):
            rows[d] = (c, v)
        newest = p
    series = [(d, c, v) for d, (c, v) in sorted(rows.items())]
    if not series or newest is None:
        return {**empty, "files": len(paths)}
    as_of = series[-1][0]
    metrics = compute_metrics(series)
    data = newest.read_bytes()
    url = newest.resolve().as_uri()
    src = session.scalars(select(Source).where(Source.url == url)).first()
    if src is None:
        src = Source(url=url, type="price", tier=PRICE_TIER)
        session.add(src)
    src.tier, src.status = PRICE_TIER, "ok"
    src.content_hash = hashlib.sha256(data).hexdigest()
    src.fetch_date = datetime.now(timezone.utc)
    src.publish_date = as_of
    session.flush()
    period = f"price_asof_{as_of.isoformat()}"
    saved = 0
    for metric, value in metrics.items():
        exists = session.scalars(select(Fact.id).where(
            Fact.instrument_id == instrument_id, Fact.metric == metric, Fact.period == period,
            Fact.value == value).limit(1)).first()
        if exists:
            continue
        session.add(Fact(instrument_id=instrument_id, source_id=src.id, metric=metric, value=value,
                         unit="percent" if metric.startswith(("price_vs", "momentum")) else "index"
                         if metric == "rsi14" else "ratio", period=period, period_end=as_of))
        saved += 1
    session.flush()
    return {"files": len(paths), "as_of": as_of.isoformat(), "metrics": metrics, "saved": saved, "source_id": src.id}
