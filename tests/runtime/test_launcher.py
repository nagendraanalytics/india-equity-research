"""`python -m ier.runtime` launcher: seed, plan, workers (--drain). Real DB, stub handlers."""
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import select

from ier.db.models import Agent, Instrument, Job
from ier.runtime.__main__ import main, run_until_drained

MACRO = "Information Technology"
CSV = Path(__file__).resolve().parents[1] / "fixtures" / "classification_sample.csv"


def _instruments(factory):
    with factory() as s:
        for i, nm in enumerate(("Alpha Ltd", "Beta Ltd")):
            s.add(Instrument(isin=f"INE00000000{i}", name=nm, macro_sector=MACRO, sector="Software",
                             industry="IT Services", status="active", listing_date=date(2020, 1, 1),
                             avg_daily_value=5e7))
        s.commit()


def _noop(session, job):
    return None


HANDLERS = {"sector": _noop, "industry": _noop, "company": _noop}


def test_load_classification_seed_and_plan_via_cli(factory, capsys):
    assert main(["load-classification", str(CSV)], session_factory=factory) == 0
    with factory() as s:
        assert s.scalars(select(Instrument).where(Instrument.isin == "INE009A01021")).first()
        # sample instruments have no listing_date/avg_daily_value: not in the universe
        s.add(Instrument(isin="INE0000000X", name="Gamma Ltd", macro_sector=MACRO, sector="Software",
                         industry="IT Services", status="active", listing_date=date(2020, 1, 1),
                         avg_daily_value=5e7))
        s.commit()
    assert main(["seed", MACRO], session_factory=factory) == 0
    with factory() as s:
        roles = {a.role for a in s.scalars(select(Agent))}
        assert {"master", "macro_sector", "sector", "industry", "company", "fundamentals",
                "synthesis"} <= roles
    assert main(["plan", MACRO, "--run-id", "r1"], session_factory=factory) == 0
    with factory() as s:
        jobs = s.scalars(select(Job)).all()
        assert len(jobs) == 3 and all(j.payload == {"run_id": "r1"} for j in jobs)
    out = capsys.readouterr().out
    assert "r1" in out


def test_plan_without_seed_is_an_error(factory):
    with pytest.raises(SystemExit) as e:
        main(["plan", MACRO], session_factory=factory)
    assert e.value.code != 0


def test_workers_drain_runs_every_job_then_returns(factory):
    _instruments(factory)
    assert main(["seed", MACRO], session_factory=factory) == 0
    assert main(["plan", MACRO, "--run-id", "r2"], session_factory=factory) == 0
    assert main(["workers", "--drain", "--timeout", "30"], session_factory=factory,
                handlers=HANDLERS) == 0
    with factory() as s:
        assert {j.status for j in s.scalars(select(Job))} == {"done"}


def test_failed_parent_does_not_block_its_dependents(factory):
    """F5: a failed sector job must not leave its industry/company jobs queued forever: the
    sector tailwind is an optional input, so dependents run (rules treat it as missing)."""
    _instruments(factory)
    main(["seed", MACRO], session_factory=factory)
    main(["plan", MACRO, "--run-id", "r3"], session_factory=factory)

    def boom(session, job):
        raise RuntimeError("boom")

    summary = run_until_drained(
        factory, {"sector": 1, "industry": 1, "company": 1}, handlers={**HANDLERS, "sector": boom},
        timeout=30, config={"retry": {"max_attempts": 1, "backoff_base_seconds": 0}})
    assert summary["failed"] == 1 and summary["queued"] == 0 and summary["done"] == 3
    assert summary["timed_out"] is False


def test_drain_reports_backoff_and_exits_nonzero_while_jobs_remain(factory, capsys):
    """F5: jobs waiting in retry backoff are reported and the exit status is non-zero."""
    _instruments(factory)
    main(["seed", MACRO], session_factory=factory)
    main(["plan", MACRO, "--run-id", "r5"], session_factory=factory)

    def boom(session, job):
        raise RuntimeError("boom")
    # default retry config: 30 s backoff, so the failed sector job waits in backoff
    code = main(["workers", "--drain", "--timeout", "20"], session_factory=factory,
                handlers={**HANDLERS, "sector": boom})
    out = capsys.readouterr().out
    assert code != 0
    assert "backoff=1" in out and "queued=4" in out   # sector in backoff, the rest wait on it


def test_drain_times_out_instead_of_hanging(factory):
    _instruments(factory)
    main(["seed", MACRO], session_factory=factory)
    main(["plan", MACRO, "--run-id", "r4"], session_factory=factory)
    import threading
    release = threading.Event()

    def slow(session, job):
        release.wait(5)

    try:
        summary = run_until_drained(factory, {"sector": 1, "industry": 1, "company": 1},
                                    handlers={**HANDLERS, "sector": slow}, timeout=0.5, grace=0.2)
        assert summary["timed_out"] is True
    finally:
        release.set()
        # let the in-flight handler finish before the fixture wipes the tables
        from tests.runtime.conftest import wait_for

        def idle():
            with factory() as s:
                return not s.scalars(select(Job).where(Job.status == "running")).first()
        assert wait_for(idle, timeout=10)
        import time
        time.sleep(0.5)


def test_unknown_command_exits_nonzero(factory):
    with pytest.raises(SystemExit) as e:
        main(["frobnicate"], session_factory=factory)
    assert e.value.code != 0


# --- F9: purge-tier3 removes pre-fix Facts from Tier 3 sources (dry run by default) -------

def _tier_facts(factory):
    from ier.db.models import Fact, Source
    with factory() as s:
        inst = Instrument(isin="INE00000T3", name="T3 Ltd", macro_sector=MACRO)
        s.add(inst)
        s.flush()
        ids = {}
        for tier in (1, 2, 3):
            src = Source(url=f"http://t{tier}.example/x", type="filing", tier=tier)
            s.add(src)
            s.flush()
            f = Fact(instrument_id=inst.id, source_id=src.id, metric="promoter_pledging",
                     value=50.0, period="FY25")
            s.add(f)
            s.flush()
            ids[tier] = f.id
        s.commit()
    return ids


def test_purge_tier3_dry_run_deletes_nothing(factory, capsys):
    from ier.db.models import Fact
    ids = _tier_facts(factory)
    assert main(["purge-tier3"], session_factory=factory) == 0
    with factory() as s:
        assert {f.id for f in s.scalars(select(Fact))} == set(ids.values())
    assert "dry run" in capsys.readouterr().out.lower()


def test_purge_tier3_apply_deletes_only_tier3_facts(factory, capsys):
    from ier.db.models import Fact
    ids = _tier_facts(factory)
    assert main(["purge-tier3", "--apply"], session_factory=factory) == 0
    with factory() as s:
        assert {f.id for f in s.scalars(select(Fact))} == {ids[1], ids[2]}
    assert "deleted 1" in capsys.readouterr().out.lower()
