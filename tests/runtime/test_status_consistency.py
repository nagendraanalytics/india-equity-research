"""emit() commits independently, so status events must never outlive the job/transaction they
describe: the tree must not show running/queued for an agent without a live job or run."""
import threading

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from ier.db.models import Agent, AgentEvent, Job
from ier.llm.router import BudgetExceeded
from ier.runtime.events import emit, seed_agents, tree
from ier.runtime.queue import claim_next, enqueue
from ier.runtime.worker import _run_job
from tests.runtime.conftest import factory  # noqa: F401
from tests.runtime.test_events import MACRO, _inst


def world(factory):  # noqa: F811
    with factory() as s:
        _inst(s, "Alpha Ltd", "Software", "IT Services")
        seed_agents(s, MACRO)
        s.commit()
        co = s.scalar(select(Agent).where(Agent.role == "company"))
        wk = s.scalar(select(Agent).where(Agent.parent_id == co.id, Agent.role == "fundamentals"))
        return co.id, wk.id


def status_of(factory, agent_id):  # noqa: F811
    with factory() as s:
        return tree(s, agent_id).status


def claimed(factory, agent_id):  # noqa: F811
    with factory() as s:
        enqueue(s, agent_id, "manual")
        s.commit()
    s = factory()
    job = claim_next(s, "company")
    s.commit()
    return s, job


@pytest.mark.parametrize("exc", [BudgetExceeded("cap"), RuntimeError("boom")])
def test_worker_agent_started_in_rolled_back_job_is_not_left_running(factory, exc):
    co, wk = world(factory)
    s, job = claimed(factory, co)

    def handler(sess, j):
        emit(sess, wk, "started", {"task": "fundamentals"})
        raise exc
    _run_job(s, job, {"company": handler}, threading.Event(), {"retry": {
        "max_attempts": 3, "backoff_base_seconds": 30}})
    s.close()
    with factory() as o:
        master = tree(o)
        assert master.status != "running"
        assert tree(o, wk).status == "failed"          # aborted run is closed, not dangling
        last = o.scalar(select(AgentEvent).where(AgentEvent.agent_id == wk)
                        .order_by(AgentEvent.id.desc()).limit(1))
        assert last.kind == "failed" and "aborted" in last.payload["error"]
        # the company agent: queued again (budget halt, live job) or failed (retry scheduled)
        assert tree(o, co).status in ("queued", "failed", "degraded")
        assert o.scalar(select(Job.status)) == "queued"


def test_queued_event_rolls_back_with_the_job(factory):
    co, _ = world(factory)
    s = factory()
    enqueue(s, co, "manual")
    s.rollback()
    s.close()
    with factory() as o:
        assert o.scalar(text("select count(*) from jobs")) == 0
        assert o.scalar(text("select count(*) from agent_events where kind='queued'")) == 0
        assert tree(o, co).status == "idle"
    s = factory()
    enqueue(s, co, "manual")
    s.commit()
    s.close()
    assert status_of(factory, co) == "queued"


def test_completed_not_logged_when_job_commit_fails(factory):
    co, _ = world(factory)

    class Flaky(Session):
        fail_next = False

        def commit(self):
            if self.fail_next:
                self.fail_next = False
                raise RuntimeError("commit failed")
            return super().commit()
    with factory() as s:
        enqueue(s, co, "manual")
        s.commit()
    s = Flaky(bind=factory.kw["bind"], expire_on_commit=False)
    job = claim_next(s, "company")
    s.commit()

    def handler(sess, j):
        sess.fail_next = True
    _run_job(s, job, {"company": handler}, threading.Event(), {"retry": {
        "max_attempts": 3, "backoff_base_seconds": 30}})
    s.close()
    with factory() as o:
        kinds = o.scalars(select(AgentEvent.kind).where(AgentEvent.agent_id == co)
                          .order_by(AgentEvent.id)).all()
        assert "completed" not in kinds
        assert o.scalar(select(Job.status)) != "done"
        assert tree(o, co).status != "completed"


@pytest.mark.parametrize("exc", [BudgetExceeded("cap"), RuntimeError("boom")])
def test_failed_sector_job_does_not_close_sibling_jobs_started_agents(factory, exc):
    co, wk = world(factory)
    with factory() as s:
        company = s.get(Agent, co)
        industry = s.get(Agent, company.parent_id)
        sector = s.get(Agent, industry.parent_id)
        ids = (sector.id, industry.id)
    sector_id, industry_id = ids
    # a company job (sibling of the sector job, no depends_on) is legitimately running
    with factory() as s:
        enqueue(s, co, "manual")
        s.commit()
    cs = factory()
    cjob = claim_next(cs, "company")
    cs.commit()
    # the sector job runs and fails; it started its own industry agent
    with factory() as s:
        enqueue(s, sector_id, "manual")
        s.commit()
    ss = factory()
    sjob = claim_next(ss, "sector")
    ss.commit()

    def handler(sess, j):
        emit(sess, industry_id, "started", {"task": "rank"})
        # while the sector job runs, the sibling company job starts its agents (after the
        # sector job began, so they are newer than any start-of-handler marker)
        emit(cs, co, "started", {"job_id": cjob.id})
        emit(cs, wk, "started", {"task": "fundamentals"})
        raise exc
    _run_job(ss, sjob, {"sector": handler}, threading.Event(), {"retry": {
        "max_attempts": 3, "backoff_base_seconds": 30}})
    ss.close()
    cs.rollback()
    cs.close()
    with factory() as o:
        def kinds(a):
            return o.scalars(select(AgentEvent.kind).where(AgentEvent.agent_id == a)
                             .order_by(AgentEvent.id)).all()
        assert kinds(co)[-1] == "started" and "failed" not in kinds(co)
        assert kinds(wk) == ["started"]
        assert tree(o, wk).status == "running"
        # the failed job's own started agent is closed
        assert kinds(industry_id)[-2:] == ["started", "failed"]
