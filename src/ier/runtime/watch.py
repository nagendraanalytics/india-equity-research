"""Watch the local filings folder and queue a company run when its files change.

Layout is the local-folder connector's: <local_dir>/<ISIN>/<file>. A folder's fingerprint is a
hash of its supported files' names, sizes and modification times (no file is read here).
First scan after a start queues every ISIN folder that has a company agent; a run whose
filings it has already processed skips itself without calling a model. Research labels only.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from ier.db.models import Instrument, Job
from ier.runtime.triggers import on_files_changed
from ier.tools.feeds import ALL_EXTENSIONS
from ier.tools.local_filings import EXTENSIONS, ISIN_RE

WATCH_EXTENSIONS = tuple(sorted(set(EXTENSIONS) | set(ALL_EXTENSIONS)))


def folder_fingerprints(local_dir: str | Path) -> dict[str, str]:
    """{ISIN: fingerprint} for each valid ISIN folder holding at least one supported file."""
    root = Path(local_dir)
    out: dict[str, str] = {}
    if not root.is_dir():
        return out
    for d in sorted(root.iterdir()):
        if d.is_symlink() or not d.is_dir() or not ISIN_RE.fullmatch(d.name):
            continue
        parts = []
        for f in sorted(d.iterdir()):
            if f.is_symlink() or not f.is_file() or f.suffix.lower() not in WATCH_EXTENSIONS:
                continue
            st = f.stat()
            parts.append(f"{f.name}|{st.st_size}|{st.st_mtime_ns}")
        if parts:
            out[d.name] = hashlib.sha256("\n".join(parts).encode()).hexdigest()
    return out


def watch_once(session: Session, local_dir: str | Path, seen: dict[str, str]) -> list[Job]:
    """Queue company jobs for ISIN folders whose fingerprint differs from `seen` (updated in
    place). Folders for an ISIN with no instrument are ignored. Flushes; never commits."""
    jobs: list[Job] = []
    for isin, fp in folder_fingerprints(local_dir).items():
        if seen.get(isin) == fp:
            continue
        iid = session.scalar(select(Instrument.id).where(Instrument.isin == isin))
        seen[isin] = fp
        if iid is not None:
            jobs.extend(on_files_changed(session, iid, fp))
    return jobs


def watch_loop(factory: Callable[[], Session], local_dir: str | Path, stop, *,
               interval: float = 15.0, log: Callable[[str], None] = print,
               scheduler: Callable[[Session], int] | None = None) -> None:
    """Scan every `interval` seconds until `stop` (a threading.Event) is set. `scheduler`
    (optional) is called with a session each scan and returns the number of source-check jobs
    it queued (see ier.sources.service.schedule_due)."""
    seen: dict[str, str] = {}
    while not stop.is_set():
        try:
            with factory() as s:
                jobs = watch_once(s, local_dir, seen)
                s.commit()
            if jobs:
                log(f"watch: {len(jobs)} company job(s) queued for changed filings")
        except Exception as exc:  # keep watching through a database hiccup
            seen.clear()
            log(f"watch: scan failed ({type(exc).__name__}: {exc}); will retry")
        if scheduler is not None:
            try:
                with factory() as s:
                    n = scheduler(s)
                    s.commit()
                if n:
                    log(f"watch: {n} source check job(s) queued")
            except Exception as exc:  # noqa: BLE001 - a source problem must not stop the folder watch
                log(f"watch: source scheduling failed ({type(exc).__name__}: {exc}); will retry")
        stop.wait(interval)
