"""F5: stalls. Failed parents do not block dependents, the status tree shows 'blocked' for a
job waiting on an unfinished parent, and stale running jobs are requeued (with events, an
attempt counted and the max-attempts cap) at worker start and on an interval."""
import threading
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from ier.agents.sector import SectorView
from ier.db.models import AgentEvent, Job
from ier.graph import company as cg
from ier.runtime import worker as worker_mod
from ier.runtime.__main__ import run_until_drained
from ier.runtime.events import seed_agents, tree
from ier.runtime.queue import claim_next, requeue_stale
from tests.db.conftest import engine, session  # noqa: F401
from tests.graph.test_company import MACRO, _child, _company, _inst, _source, stubs  # noqa: F401
from tests.runtime.conftest import factory, wait_for  # noqa: F401

TIERS = {"sector": 1, "industry": 1, "company": 1}


def _world(s):
    a = _inst(s, "Alpha Ltd")
    _source(s, a)
    seed_agents(s, MACRO)
    return a


def _find(node, name):
    if node.name == name:
        return node
    for c in node.children:
        r = _find(c, name)
        if r:
            return r
    return None


def test_failed_sector_does_not_block_companies(factory, monkeypatch, stubs):
    with factory() as s:
        _world(s)
        cg.plan_run(s, MACRO, "run-b")
        s.commit()

    def boom(session, sector, run_id, **kw):
        raise RuntimeError("SchemaError stand-in")
    monkeypatch.setattr(cg, "analyse_sector", boom)
    monkeypatch.setattr(cg, "rank_industry", lambda s, ind: [])
    h = {}
    cg.register_default_handlers(h)
    summary = run_until_drained(factory, TIERS, handlers=h, timeout=20,
                                config={"retry": {"max_attempts": 1, "backoff_base_seconds": 0}})
    assert summary["queued"] == 0 and summary["failed"] == 1 and summary["done"] == 2
    with factory() as s:
        root = tree(s)
        assert _find(root, "Software").status == "failed"
        assert _find(root, "Alpha Ltd").status == "completed"


def test_tree_shows_blocked_for_jobs_waiting_on_an_unfinished_parent(session):
    s = session
    _world(s)
    cg.plan_run(s, MACRO, "run-t")
    root = tree(s)
    assert _find(root, "Software").status == "queued"        # runnable now
    assert _find(root, "IT Services").status == "blocked"    # waits for the sector job
    assert _find(root, "Alpha Ltd").status == "blocked"      # waits for the industry job
    assert root.status == "queued"                           # ancestors: pending work


def _stale_running_job(factory, age=timedelta(days=1), attempts=0):
    with factory() as s:
        a = _world(s)
        comp = _company(s, a)
        wk = _child(s, comp.id, "fundamentals")
        job = Job(agent_id=comp.id, trigger="manual", status="running", attempts=attempts,
                  scheduled_at=datetime.now(timezone.utc) - age,
                  claimed_at=datetime.now(timezone.utc) - age)
        s.add(job)
        s.flush()
        from ier.runtime.events import emit
        emit(s, comp.id, "started", {"job_id": job.id})
        emit(s, wk.id, "started", {"task": "fundamentals"})
        s.commit()
        return job.id, comp.id, wk.id


def test_requeue_stale_counts_an_attempt_writes_events_and_closes_started(factory):
    jid, comp, wk = _stale_running_job(factory)
    with factory() as s:
        assert requeue_stale(s, timedelta(hours=1)) == 1
        s.commit()
    with factory() as s:
        job = s.get(Job, jid)
        assert job.status == "queued" and job.attempts == 1 and job.claimed_at is None
        assert "stale" in job.last_error
        last = {e.agent_id: e for e in s.scalars(select(AgentEvent).order_by(AgentEvent.id))}
        assert last[comp].kind == "queued" and last[comp].payload["reason"] == "stale"
        assert last[wk].kind == "failed" and "aborted" in last[wk].payload["error"]
        assert tree(s, comp).status == "degraded"   # aborted worker shown, not left running


def test_requeue_stale_fails_the_job_at_max_attempts(factory):
    jid, comp, _ = _stale_running_job(factory, attempts=2)
    with factory() as s:
        requeue_stale(s, timedelta(hours=1), config={"retry": {"max_attempts": 3,
                                                                "backoff_base_seconds": 0}})
        s.commit()
    with factory() as s:
        assert s.get(Job, jid).status == "failed" and s.get(Job, jid).attempts == 3
        last = s.scalars(select(AgentEvent).where(AgentEvent.agent_id == comp)
                         .order_by(AgentEvent.id.desc()).limit(1)).one()
        assert last.kind == "failed"


def test_workers_requeue_stale_jobs_at_start(factory):
    jid, comp, _ = _stale_running_job(factory)
    ran = []
    summary = run_until_drained(factory, {"company": 1},
                                handlers={"company": lambda s, j: ran.append(j.id)}, timeout=10,
                                config={"retry": {"max_attempts": 3, "backoff_base_seconds": 0},
                                        "stale": {"after_seconds": 3600,
                                                  "check_interval_seconds": 3600}})
    assert summary["timed_out"] is False and summary["running"] == 0
    assert ran == [jid] and summary["done"] == 1


def test_workers_requeue_stale_jobs_on_an_interval(factory):
    stop = threading.Event()
    cfg = {"retry": {"max_attempts": 3, "backoff_base_seconds": 0},
           "stale": {"after_seconds": 0.5, "check_interval_seconds": 0.1}}
    ran = []
    t = threading.Thread(target=worker_mod.run_workers, args=({"company": 1}, stop), daemon=True,
                         kwargs=dict(session_factory=factory, config=cfg, poll_interval=0.05,
                                     handlers={"company": lambda s, j: ran.append(j.id)}))
    t.start()
    try:
        # a job that becomes stale only after the workers started
        jid, _, _ = _stale_running_job(factory, age=timedelta(seconds=0))
        assert wait_for(lambda: ran == [jid], timeout=10)
    finally:
        stop.set()
        t.join(5)


def test_claim_after_failed_parent(session):
    s = session
    _world(s)
    jobs = cg.plan_run(s, MACRO, "run-c")
    sector_job = jobs[0]
    assert claim_next(s, "industry") is None          # parent queued: blocked
    sector_job.status = "failed"
    s.flush()
    assert claim_next(s, "industry") is not None     # parent failed: runs without it
    n = s.scalar(select(func.count()).select_from(Job).where(Job.status == "running"))
    assert n == 1
