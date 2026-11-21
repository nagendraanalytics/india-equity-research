"""Local-folder filings connector: filings the owner downloaded by hand.

Layout: <local_dir>/<ISIN>/<filing_type>__<period>__<YYYY-MM-DD>.<ext>
  e.g.  data/filings/INE009A01021/annual_report__FY2025__2025-06-10.pdf
        data/filings/INE009A01021/results__Q2FY27__2026-10-16.pdf
The date part is optional. Extensions: .pdf, .txt, .htm, .html. Anything else is ignored.

Every file becomes a Source (type "local", url = file URI, tier = the configured local_tier)
so provenance works as for web fetches. No network access, no redirects. Files must sit
inside local_dir (symlinks and path tricks are refused) and be under the size cap.
"""
from __future__ import annotations

import hashlib
import html
import io
import logging
import re
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import url2pathname

from sqlalchemy import select
from sqlalchemy.orm import Session

from ier.db.models import Source
from ier.tools.base import FetchRefused, FetchResult

log = logging.getLogger(__name__)
ISIN_RE = re.compile(r"[A-Z0-9]{12}")
EXTENSIONS = (".pdf", ".txt", ".htm", ".html")
FEED_TYPES = ("prices", "news", "social")  # see ier.tools.feeds
DEFAULT_MAX_BYTES = 20 * 1024 * 1024
DEFAULT_TIER = 2  # conservative; set local_tier: 1 only for official exchange/company filings


def _root(local_dir: str) -> Path:
    root = Path(local_dir).expanduser().resolve()
    if not root.is_dir():
        raise FetchRefused(f"local_dir {str(root)!r} is not a folder")
    return root


def _parse_name(stem: str) -> tuple[str, str, date | None]:
    parts = stem.split("__")
    if len(parts) not in (2, 3) or not parts[0].strip() or not parts[1].strip():
        raise ValueError(f"name must be <filing_type>__<period>[__YYYY-MM-DD], got {stem!r}")
    published = date.fromisoformat(parts[2]) if len(parts) == 3 else None
    return parts[0].strip(), parts[1].strip(), published


def list_local(isin: str, local_dir: str):
    """FilingRefs for the files in <local_dir>/<isin>/, newest first. [] if no folder."""
    from ier.tools.exchange_filings import FilingRef  # avoid an import cycle at load time
    if not ISIN_RE.fullmatch(isin or ""):
        return []
    root = _root(local_dir)
    folder = (root / isin)
    if not folder.is_dir() or folder.is_symlink():
        return []
    refs = []
    for path in sorted(folder.iterdir()):
        if path.suffix.lower() not in EXTENSIONS or path.is_symlink() or not path.is_file():
            continue
        try:
            filing_type, period, published = _parse_name(path.stem)
        except ValueError as exc:
            log.warning("ignoring %s: %s", path.name, exc)
            continue
        if filing_type.lower() in FEED_TYPES:   # prices/news/social are feeds, not filings
            continue
        refs.append(FilingRef(isin=isin, url=path.resolve().as_uri(), filing_type=filing_type,
                              period=period, published_date=published))
    return sorted(refs, key=lambda r: (r.published_date or date.min, r.url), reverse=True)


def _to_text(path: Path, data: bytes) -> str:
    ext = path.suffix.lower()
    if ext == ".pdf":
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        return "\n".join((page.extract_text() or "") for page in reader.pages)
    text = data.decode("utf-8", errors="replace")
    if ext in (".htm", ".html"):
        text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text)
        text = html.unescape(re.sub(r"(?s)<[^>]+>", " ", text))
    return text


def ingest_local(url: str, session: Session, *, local_dir: str, tier: int = DEFAULT_TIER,
                 max_bytes: int = DEFAULT_MAX_BYTES) -> FetchResult:
    """Read one local filing and log it as a Source; return its extracted text.

    Mirrors ier.tools.base.fetch: `changed` is True when the file's bytes differ from the last
    ingest; an unreadable/empty file gives empty content and marks the Source stale. The caller
    owns the transaction (this flushes, never commits)."""
    if tier not in (1, 2, 3):
        raise ValueError(f"local_tier must be 1-3, got {tier!r}")
    parts = urlsplit(url)
    if parts.scheme != "file":
        raise FetchRefused(f"not a file URL: {url!r}")
    path = Path(url2pathname(parts.path))
    root = _root(local_dir)
    if path.is_symlink() or not path.resolve().is_relative_to(root):
        raise FetchRefused(f"{url!r} is outside local_dir")
    if path.suffix.lower() not in EXTENSIONS:
        raise FetchRefused(f"unsupported file type: {path.suffix!r}")
    if path.stat().st_size > max_bytes:
        raise FetchRefused(f"{path.name} is larger than {max_bytes} bytes")

    data = path.read_bytes()
    src = session.scalars(select(Source).where(Source.url == url)).first()
    if src is None:
        src = Source(url=url, type="local", tier=tier)
        session.add(src)
    else:
        src.tier = tier  # the config decides how far local files are trusted
    try:
        text = _to_text(path, data)
    except Exception:  # noqa: BLE001 - corrupt PDF etc.: stale, skip, never crash the run
        log.warning("could not extract text from %s", path.name, exc_info=True)
        text = ""
    if not text.strip():
        src.status = "stale"
        session.flush()
        return FetchResult(src.id, b"", False)
    digest = hashlib.sha256(data).hexdigest()
    changed = src.content_hash != digest
    src.content_hash = digest
    src.fetch_date = datetime.now(timezone.utc)
    src.status = "ok"
    session.flush()
    return FetchResult(src.id, text.encode("utf-8"), changed)
