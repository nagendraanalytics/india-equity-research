"""F7: concurrency. Two concurrent plan_run calls for one run_id create each job once, and two
jobs of one agent never run at the same time."""
import threading
import time

from sqlalchemy import func, select

from ier.db.models import AgentEvent, Job
from ier.graph import company as cg
from ier.runtime.events import seed_agents
from ier.runtime.queue import claim_next, enqueue
from ier.runtime.worker import run_workers
from tests.db.conftest import engine, session  # noqa: F401
from tests.graph.test_company import MACRO, _company, _inst, _source
from tests.runtime.conftest import factory, wait_for  # noqa: F401


def _world(factory):
    with factory() as s:
        a = _inst(s, "Alpha Ltd")
        b = _inst(s, "Beta Ltd")
        _source(s, a)
        _source(s, b)
        seed_agents(s, MACRO)
        s.commit()
        return _company(s, a).id


def test_concurrent_plan_runs_for_one_run_id_create_each_job_once(factory):
    _world(factory)
    s1, s2 = factory(), factory()
    j1 = cg.plan_run(s1, MACRO, "same-run")
    done = threading.Event()

    def second():
        cg.plan_run(s2, MACRO, "same-run")   # blocks on s1's uncommitted rows, then no-ops
        s2.commit()
        done.set()
    t = threading.Thread(target=second, daemon=True)
    t.start()
    time.sleep(0.3)
    s1.commit()
    t.join(10)
    assert done.is_set()
    s1.close()
    s2.close()
    with factory() as s:
        assert s.scalar(select(func.count()).select_from(Job)) == len(j1) == 4
        # one queued event per job, none for a discarded duplicate
        assert s.scalar(select(func.count()).select_from(AgentEvent)
                        .where(AgentEvent.kind == "queued")) == 4


def test_plan_run_returns_the_other_sessions_jobs_when_they_exist(factory):
    _world(factory)
    with factory() as s1:
        first = cg.plan_run(s1, MACRO, "r")
        s1.commit()
        ids = [j.id for j in first]
    with factory() as s2:
        assert [j.id for j in cg.plan_run(s2, MACRO, "r")] == ids


def test_claim_next_skips_an_agent_that_already_has_a_running_job(session):
    s = session
    a = _inst(s, "Solo Ltd")
    seed_agents(s, MACRO)
    comp = _company(s, a).id
    first = enqueue(s, comp, "manual")
    enqueue(s, comp, "manual")
    assert claim_next(s, "company").id == first.id
    assert claim_next(s, "company") is None          # same agent: waits for the running job


def test_two_jobs_for_one_agent_never_run_concurrently(factory):
    comp = _world(factory)
    with factory() as s:
        for _ in range(3):
            enqueue(s, comp, "manual")
        s.commit()
    active, peak, lock = [0], [0], threading.Lock()

    def handler(session, job):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        time.sleep(0.2)
        with lock:
            active[0] -= 1
    stop = threading.Event()
    t = threading.Thread(target=run_workers, args=({"company": 3}, stop), daemon=True,
                         kwargs=dict(session_factory=factory, handlers={"company": handler},
                                     poll_interval=0.01))
    t.start()
    try:
        def all_done():
            with factory() as s:
                return s.scalar(select(func.count()).select_from(Job)
                                .where(Job.status == "done")) == 3
        assert wait_for(all_done, timeout=10)
    finally:
        stop.set()
        t.join(5)
    assert peak[0] == 1


def test_claim_next_never_runs_two_jobs_of_one_agent_under_contention(factory):
    """Stress: 12 claimants, 3 agents, many jobs each. The agent-busy filter alone is evaluated
    on a pre-claim snapshot, so claim_next must re-check after taking the row locks."""
    from sqlalchemy import text

    from ier.db.models import Agent
    from ier.runtime.queue import complete, tier_number
    with factory() as s:
        agent_ids = []
        for i in range(3):
            a = Agent(role="company", tier=tier_number("company"), scope_name=f"stress{i}")
            s.add(a)
            s.flush()
            agent_ids.append(a.id)
        for k in range(120):
            enqueue(s, agent_ids[k % 3], "manual")
        s.commit()
    overlaps: list = []
    stop = threading.Event()

    def watcher():
        with factory() as s:
            while not stop.is_set():
                rows = s.execute(text("select agent_id, count(*) from jobs where status='running' "
                                      "group by agent_id having count(*) > 1")).all()
                if rows:
                    overlaps.append(rows)
                s.commit()

    def worker():
        s = factory()
        try:
            while True:
                j = claim_next(s, "company")
                s.commit()
                if j is None:
                    left = s.scalar(select(func.count()).select_from(Job).where(Job.status == "queued"))
                    s.commit()
                    if left == 0:
                        return
                    continue
                time.sleep(0.01)   # hold the job running so overlaps are observable
                complete(s, j)
                s.commit()
        finally:
            s.close()
    w = threading.Thread(target=watcher, daemon=True)
    w.start()
    ts = [threading.Thread(target=worker) for _ in range(12)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(120)
    stop.set()
    w.join(5)
    assert not any(t.is_alive() for t in ts), "claimants did not drain the queue"
    assert not overlaps, overlaps[:3]
