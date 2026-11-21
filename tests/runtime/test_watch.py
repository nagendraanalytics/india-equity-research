"""Folder watcher: fingerprints, change-only queueing, absorbing repeats, ignoring junk."""
import os
from datetime import date

from sqlalchemy import select

from ier.db.models import Agent, Instrument, Job
from ier.runtime.events import seed_agents
from ier.runtime.watch import folder_fingerprints, watch_once

MACRO = "Information Technology"
ISIN = "INE009A01021"


def _setup(factory):
    with factory() as s:
        s.add(Instrument(isin=ISIN, name="Infosys", macro_sector=MACRO, sector="Software",
                         industry="IT Services", status="active", listing_date=date(2020, 1, 1),
                         avg_daily_value=5e7))
        s.flush()
        seed_agents(s, MACRO)
        s.commit()


def _file(root, isin, name, text="x"):
    d = root / isin
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(text)


def _jobs(factory):
    with factory() as s:
        return s.scalars(select(Job).where(Job.trigger == "new_file").order_by(Job.id)).all()


def test_fingerprint_changes_with_content_and_ignores_junk(tmp_path):
    _file(tmp_path, ISIN, "results__Q2__2026-10-16.txt", "a")
    _file(tmp_path, ISIN, "notes.docx", "ignored")
    _file(tmp_path, "not-an-isin", "results__Q2.txt")
    (tmp_path / "INE000000000").mkdir()      # empty folder
    first = folder_fingerprints(tmp_path)
    assert list(first) == [ISIN]
    _file(tmp_path, ISIN, "results__Q2__2026-10-16.txt", "longer text")
    assert folder_fingerprints(tmp_path)[ISIN] != first[ISIN]
    assert folder_fingerprints(tmp_path / "missing") == {}


def test_watch_queues_once_per_change_and_absorbs_repeats(factory, tmp_path):
    _setup(factory)
    _file(tmp_path, ISIN, "results__Q2__2026-10-16.txt", "a")
    seen: dict = {}
    with factory() as s:
        assert len(watch_once(s, tmp_path, seen)) == 1
        s.commit()
    with factory() as s:
        assert watch_once(s, tmp_path, seen) == []        # nothing changed
        s.commit()
    f = tmp_path / ISIN / "results__Q2__2026-10-16.txt"
    f.write_text("changed")
    os.utime(f, ns=(1, 2_000_000_000))                    # force a different mtime
    with factory() as s:
        assert watch_once(s, tmp_path, seen) == []        # still queued: absorbed, payload refreshed
        s.commit()
    jobs = _jobs(factory)
    assert len(jobs) == 1 and jobs[0].payload["fingerprint"] == seen[ISIN]
    assert jobs[0].payload.get("force") is None


def test_watch_ignores_folder_without_instrument(factory, tmp_path):
    _setup(factory)
    _file(tmp_path, "INE000ZZZ999", "results__Q2.txt")
    with factory() as s:
        assert watch_once(s, tmp_path, {}) == []


def test_feed_files_change_the_fingerprint(tmp_path):
    _file(tmp_path, ISIN, "results__Q2FY27__2026-10-16.txt")
    before = folder_fingerprints(tmp_path)[ISIN]
    _file(tmp_path, ISIN, "prices__daily__2026-11-13.csv", "Date,Close\n2026-11-13,100\n")
    assert folder_fingerprints(tmp_path)[ISIN] != before
    _file(tmp_path, ISIN, "social__x__2026-11-13.json", "{}")
    assert folder_fingerprints(tmp_path)[ISIN] != before
