"""Feed files the owner drops next to the filings: prices, news and social posts.

Layout: <local_dir>/<ISIN>/<kind>__<period>[__YYYY-MM-DD].<ext>, kind in FEED_KINDS
  e.g.  prices__daily__2026-11-13.csv    news__announcements__2026-11-12.txt
        social__x-export__2026-11-12.csv
They are NOT filings: the filings connector skips these kinds, so they are never embedded or
used as evidence for facts. Same safety rules as local filings: files must sit inside local_dir,
no symlinks, a size cap, no network.
"""
from __future__ import annotations

import hashlib
import html
import io
import re
from pathlib import Path

from ier.tools.local_filings import ISIN_RE, _parse_name, _root

PRICES, NEWS, SOCIAL = "prices", "news", "social"
FEED_KINDS = (PRICES, NEWS, SOCIAL)
EXTENSIONS = {PRICES: (".csv",), NEWS: (".txt", ".htm", ".html", ".pdf", ".csv"),
              SOCIAL: (".csv", ".txt", ".json")}
ALL_EXTENSIONS = tuple(sorted({e for v in EXTENSIONS.values() for e in v}))
MAX_BYTES = 5 * 1024 * 1024
MAX_TEXT_CHARS = 12_000


def list_feed_files(isin: str, local_dir: str, kind: str) -> list[Path]:
    """Files of one feed kind for `isin`, oldest first (by file name). [] if no folder."""
    if kind not in FEED_KINDS or not ISIN_RE.fullmatch(isin or ""):
        return []
    root = _root(local_dir)
    folder = root / isin
    if not folder.is_dir() or folder.is_symlink():
        return []
    out = []
    for path in sorted(folder.iterdir()):
        if path.is_symlink() or not path.is_file() or path.suffix.lower() not in EXTENSIONS[kind]:
            continue
        try:
            ftype, _, _ = _parse_name(path.stem)
        except ValueError:
            continue
        if ftype.lower() != kind or not path.resolve().is_relative_to(root):
            continue
        if path.stat().st_size > MAX_BYTES:
            continue
        out.append(path)
    return out


def feeds_signature(isin: str, local_dir: str | None) -> list[list[str]]:
    """[[file name, sha256], ...] over every feed file: part of the run fingerprint, so a new or
    changed feed file wakes the run and an unchanged folder does not."""
    if not local_dir:
        return []
    try:
        return [[p.name, hashlib.sha256(p.read_bytes()).hexdigest()]
                for kind in FEED_KINDS for p in list_feed_files(isin, local_dir, kind)]
    except Exception:  # noqa: BLE001 - an unreadable folder must not break a run
        return []


def read_feed_text(path: Path) -> str:
    """Plain text of a news/social file, capped at MAX_TEXT_CHARS. JSON/CSV/TXT are read as text."""
    data = path.read_bytes()
    ext = path.suffix.lower()
    if ext == ".pdf":
        from pypdf import PdfReader
        text = "\n".join((pg.extract_text() or "") for pg in PdfReader(io.BytesIO(data)).pages)
    else:
        text = data.decode("utf-8", errors="replace")
        if ext in (".htm", ".html"):
            text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text)
            text = html.unescape(re.sub(r"(?s)<[^>]+>", " ", text))
    return text[:MAX_TEXT_CHARS]
