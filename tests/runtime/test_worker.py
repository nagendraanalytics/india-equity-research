import threading

import pytest
from datetime import timedelta
from sqlalchemy.exc import OperationalError

from ier.db.models import Agent, AgentEvent, Job
from ier.llm.router import BudgetExceeded
from ier.runtime import worker as worker_mod
from ier.runtime.queue import enqueue, requeue_stale, tier_number
from ier.runtime.worker import run_workers
from tests.runtime.conftest import wait_for

NO_BACKOFF = {"retry": {"max_attempts": 3, "backoff_base_seconds": 0}}


def _seed(factory, spec):
    """spec: list of (tier, role, n_jobs). Returns {role: [job ids]}. One agent per job: an
    agent never runs two jobs at once (final review F7), so parallelism needs distinct agents."""
    out = {}
    with factory() as s:
        for tier, role, n in spec:
            for _ in range(n):
                a = Agent(tier=tier_number(tier), role=role)
                s.add(a)
                s.flush()
                out.setdefault(role, []).append(enqueue(s, a.id, "manual").id)
        s.commit()
    return out


def _start(tiers, stop, **kw):
    t = threading.Thread(target=run_workers, args=(tiers, stop), kwargs=kw, daemon=True)
    t.start()
    return t


def _statuses(factory, ids=None):
    with factory() as s:
        q = s.query(Job)
        if ids is not None:
            q = q.filter(Job.id.in_(ids))
        return {j.id: (j.status, j.attempts) for j in q}


def _finish(t, stop):
    stop.set()
    t.join(5)
    assert not t.is_alive()


def test_concurrency_limit_per_tier_respected(factory):
    _seed(factory, [("company", "company", 6), ("sector", "sector", 3)])
    lock = threading.Lock()
    running = {"company": 0, "sector": 0}
    peak = {"company": 0, "sector": 0}
    company_full = threading.Event()
    gate = threading.Event()

    def make(tier, limit):
        def handler(session, job):
            with lock:
                running[tier] += 1
                peak[tier] = max(peak[tier], running[tier])
                if tier == "company" and running[tier] == limit:
                    company_full.set()
            gate.wait(5)
            with lock:
                running[tier] -= 1
        return handler

    stop = threading.Event()
    t = _start({"company": 2, "sector": 1}, stop, session_factory=factory,
               handlers={"company": make("company", 2), "sector": make("sector", 1)},
               poll_interval=0.01)
    try:
        assert company_full.wait(5)
        gate.set()
        assert wait_for(lambda: all(st == "done" for st, _ in _statuses(factory).values()))
    finally:
        gate.set()
        _finish(t, stop)
    assert peak == {"company": 2, "sector": 1}
    with factory() as s:
        kinds = sorted(e.kind for e in s.query(AgentEvent))
    assert kinds == ["completed"] * 9 + ["queued"] * 9 + ["started"] * 9   # enqueue emits queued


def test_budget_exceeded_halts_all_workers_and_leaves_jobs_queued(factory):
    ids = _seed(factory, [("company", "company", 3), ("sector", "sector", 2)])
    sector_started = threading.Event()
    halt = threading.Event()

    def sector_handler(session, job):
        sector_started.set()
        assert halt.wait(5)          # in flight when the halt happens; finishes normally

    def company_handler(session, job):
        assert sector_started.wait(5)
        raise BudgetExceeded("per_day max_tokens cap reached")

    stop = threading.Event()
    t = _start({"company": 2, "sector": 1}, stop, session_factory=factory,
               handlers={"company": company_handler, "sector": sector_handler},
               poll_interval=0.01, halt=halt)
    t.join(5)
    assert not t.is_alive(), "run_workers must return on its own after BudgetExceeded"
    assert not stop.is_set() and halt.is_set()
    company = _statuses(factory, ids["company"])
    assert all(v == ("queued", 0) for v in company.values())
    sector = sorted(_statuses(factory, ids["sector"]).values())
    assert sector == [("done", 0), ("queued", 0)]
    with factory() as s:
        assert all(j.claimed_at is None for j in s.query(Job).filter(Job.status == "queued"))


def test_failed_job_retries_then_marks_failed(factory):
    [jid] = _seed(factory, [("company", "company", 1)])["company"]
    calls = []

    def handler(session, job):
        calls.append(job.id)
        raise ValueError("parse error")

    stop = threading.Event()
    t = _start({"company": 1}, stop, session_factory=factory, handlers={"company": handler},
               poll_interval=0.01, config=NO_BACKOFF)
    try:
        assert wait_for(lambda: _statuses(factory)[jid][0] == "failed")
    finally:
        _finish(t, stop)
    assert calls == [jid] * 3
    with factory() as s:
        job = s.get(Job, jid)
        assert job.attempts == 3 and "ValueError: parse error" in job.last_error
        kinds = [e.kind for e in s.query(AgentEvent).order_by(AgentEvent.id)]
    assert kinds == ["queued"] + ["started", "failed"] * 3


def test_handler_rollback_on_failure(factory):
    [jid] = _seed(factory, [("company", "company", 1)])["company"]

    def handler(session, job):
        session.add(Agent(tier=1, role="leak"))
        session.flush()
        raise RuntimeError("x")

    stop = threading.Event()
    t = _start({"company": 1}, stop, session_factory=factory, handlers={"company": handler},
               poll_interval=0.01, config={"retry": {"max_attempts": 1, "backoff_base_seconds": 0}})
    try:
        assert wait_for(lambda: _statuses(factory)[jid][0] == "failed")
    finally:
        _finish(t, stop)
    with factory() as s:
        assert s.query(Agent).filter_by(role="leak").count() == 0


def test_job_without_handler_fails_with_clear_error(factory):
    [jid] = _seed(factory, [("company", "nobody", 1)])["nobody"]
    stop = threading.Event()
    t = _start({"company": 1}, stop, session_factory=factory, handlers={},
               poll_interval=0.01, config=NO_BACKOFF)
    try:
        assert wait_for(lambda: _statuses(factory)[jid][0] == "failed")
    finally:
        _finish(t, stop)
    with factory() as s:
        job = s.get(Job, jid)
        assert job.attempts == 3 and "no handler" in job.last_error and "nobody" in job.last_error


def test_workers_stop_promptly_when_stop_set(factory):
    stop = threading.Event()
    t = _start({"company": 2, "sector": 1}, stop, session_factory=factory, handlers={},
               poll_interval=30)
    assert wait_for(lambda: sum(th.name.startswith("ier-worker") for th in threading.enumerate()) == 3)
    stop.set()
    t.join(2)
    assert not t.is_alive()
    assert not any(th.name.startswith("ier-worker") for th in threading.enumerate())


def test_run_workers_rejects_unknown_tier(factory):
    with pytest.raises(ValueError):
        run_workers({"galaxy": 1}, threading.Event(), session_factory=factory)


def test_worker_survives_db_error_in_failure_path(factory, monkeypatch):
    ids = _seed(factory, [("company", "company", 2)])["company"]
    real_fail = worker_mod.fail
    blew_up = []

    def flaky_fail(session, job, error, **kw):
        if not blew_up:
            blew_up.append(job.id)
            raise OperationalError("UPDATE jobs", {}, Exception("connection reset"))
        return real_fail(session, job, error, **kw)

    monkeypatch.setattr(worker_mod, "fail", flaky_fail)

    def handler(session, job):
        if job.id == ids[0]:
            raise ValueError("bad data")

    stop = threading.Event()
    t = _start({"company": 1}, stop, session_factory=factory, handlers={"company": handler},
               poll_interval=0.01, config=NO_BACKOFF)
    try:
        # the same single worker thread keeps going and completes the next job
        assert wait_for(lambda: _statuses(factory)[ids[1]][0] == "done")
        assert t.is_alive()
    finally:
        _finish(t, stop)
    assert blew_up == [ids[0]]
    # the job whose failure could not be recorded stays running until requeue_stale
    assert _statuses(factory)[ids[0]] == ("running", 0)
    with factory() as s:
        assert requeue_stale(s, timedelta(0)) == 1
        s.commit()
    assert _statuses(factory)[ids[0]] == ("queued", 1)   # F5: a recovered job counts an attempt


def test_worker_survives_when_rollback_itself_raises(factory):
    from sqlalchemy.orm import Session, sessionmaker

    ids = _seed(factory, [("company", "company", 2)])["company"]
    broken = {"left": 2}  # _run_job's rollback and the worker's fallback rollback both fail

    class FlakySession(Session):
        def rollback(self):
            if broken["left"] > 0:
                broken["left"] -= 1
                raise OperationalError("ROLLBACK", {}, Exception("server closed the connection"))
            return super().rollback()

    flaky = sessionmaker(factory.kw["bind"], class_=FlakySession, expire_on_commit=False)

    def handler(session, job):
        if job.id == ids[0]:
            raise ValueError("bad data")

    stop = threading.Event()
    t = _start({"company": 1}, stop, session_factory=flaky, handlers={"company": handler},
               poll_interval=0.01, config=NO_BACKOFF)
    try:
        assert wait_for(lambda: _statuses(factory)[ids[1]][0] == "done")
        assert t.is_alive()
    finally:
        _finish(t, stop)
    assert broken["left"] == 0
    # nothing could be recorded for the first job: it stays running for requeue_stale
    assert _statuses(factory)[ids[0]] == ("running", 0)
