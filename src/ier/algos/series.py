"""Loading price bars (company files) and index bars (series_points)."""
from __future__ import annotations

import math
from datetime import date
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from ier.algos.core import Bar
from ier.db.models import SeriesPoint
from ier.agents.technicals import parse_prices
from ier.tools.feeds import PRICES, list_feed_files
from ier.tools.local_filings import FetchRefused


def clean_bars(rows: Iterable[tuple[date, float, float | None]]) -> list[Bar]:
    out: dict[date, Bar] = {}
    for d, c, v in rows:
        if c is None or not math.isfinite(c) or c <= 0:
            continue
        if v is not None and (not math.isfinite(v) or v < 0):
            v = None
        out[d] = Bar(d, float(c), v)
    return [out[d] for d in sorted(out)]


def load_price_bars(isin: str, local_dir: str | None) -> tuple[list[Bar], int | None]:
    """(bars oldest first, number of price files) or ([], None) when there is nothing to read."""
    if not local_dir:
        return [], None
    try:
        paths = list_feed_files(isin, local_dir, PRICES)
    except FetchRefused:
        return [], None
    if not paths:
        return [], None
    rows: list[tuple[date, float, float | None]] = []
    for p in paths:
        rows.extend(parse_prices(p.read_text(encoding="utf-8", errors="replace")))
    return clean_bars(rows), len(paths)


def load_index_bars(session: Session, series_id: str) -> list[Bar]:
    pts = session.execute(
        select(SeriesPoint.day, SeriesPoint.value).where(SeriesPoint.series_id == series_id).order_by(SeriesPoint.day)
    ).all()
    return clean_bars((d, v, None) for d, v in pts)
