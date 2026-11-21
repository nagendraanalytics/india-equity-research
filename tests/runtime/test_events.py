import os
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import DBAPIError

from ier.db.models import Agent, AgentEvent, AppendOnlyError, Instrument
from ier.runtime.events import AgentNode, emit, seed_agents, tree
from ier.runtime.queue import enqueue, release, tier_number
from tests.db.conftest import ROOT, TEST_URL, assert_safe_test_db

MACRO = "Information Technology"
OLD = date(2020, 1, 1)
_n = 0


def _inst(session, name, sector, industry, macro=MACRO, **kw):
    global _n
    _n += 1
    d = dict(isin=f"INE{_n:09d}", name=name, macro_sector=macro, sector=sector,
             industry=industry, status="active", listing_date=OLD, avg_daily_value=5e7)
    d.update(kw)
    i = Instrument(**d)
    session.add(i)
    session.flush()
    return i


def _small_universe(session):
    _inst(session, "Alpha Ltd", "Software", "IT Services")
    _inst(session, "Beta Ltd", "Software", "IT Services")
    _inst(session, "Gamma Ltd", "Software", "Products")
    _inst(session, "Delta Ltd", "Hardware", "Devices")
    _inst(session, "Other Ltd", "Banks", "Private Banks", macro="Financials")
    _inst(session, "Suspended Ltd", "Software", "IT Services", status="suspended")


def _find(node, name):
    if node.name == name:
        return node
    for c in node.children:
        r = _find(c, name)
        if r:
            return r
    return None


def _walk(node):
    yield node
    for c in node.children:
        yield from _walk(c)


@pytest.fixture
def seeded(session):
    _small_universe(session)
    seed_agents(session, MACRO)
    return session


def _company(session, name):
    inst = session.query(Instrument).filter_by(name=name).one()
    return session.query(Agent).filter_by(scope_instrument_id=inst.id, role="company").one()


def _worker(session, name, role="fundamentals"):
    c = _company(session, name)
    return session.query(Agent).filter_by(parent_id=c.id, role=role).one()


# --- tree ------------------------------------------------------------------------------

def test_tree_built_from_taxonomy(seeded):
    s = seeded
    root = tree(s)
    assert isinstance(root, AgentNode)
    assert (root.name, root.tier) == ("master", tier_number("master"))
    [macro] = root.children
    assert (macro.name, macro.tier) == (MACRO, tier_number("macro_sector"))
    assert sorted(c.name for c in macro.children) == ["Hardware", "Software"]
    software = _find(root, "Software")
    assert software.tier == tier_number("sector")
    assert sorted(c.name for c in software.children) == ["IT Services", "Products"]
    its = _find(root, "IT Services")
    assert its.tier == tier_number("industry")
    assert sorted(c.name for c in its.children) == ["Alpha Ltd", "Beta Ltd"]   # suspended excluded
    alpha = _find(root, "Alpha Ltd")
    assert alpha.tier == tier_number("company")
    assert sorted(c.name for c in alpha.children) == ["fundamentals", "synthesis"]
    assert all(c.tier == tier_number("worker") and c.children == () for c in alpha.children)
    assert _find(root, "Other Ltd") is None and _find(root, "Suspended Ltd") is None
    # parent links in the table agree with the tree
    agents = {a.id: a for a in s.query(Agent)}
    assert agents[root.id].parent_id is None
    for n in _walk(root):
        for c in n.children:
            assert agents[c.id].parent_id == n.id
    assert len(agents) == 1 + 1 + 2 + 3 + 4 + 8
    comp = _company(s, "Alpha Ltd")
    assert comp.scope_instrument_id is not None and comp.role == "company"
    assert _find(root, "Software").id == s.query(Agent).filter_by(role="sector", scope_name="Software").one().id


def test_parent_status_running_if_any_child_running(seeded):
    s = seeded
    w = _worker(s, "Alpha Ltd")
    emit(s, w.id, "started", {"job_id": 1, "task": "analyse Alpha"})
    root = tree(s)
    for name in ("Alpha Ltd", "IT Services", "Software", MACRO, "master"):
        assert _find(root, name).status == "running", name
    assert _find(root, "Beta Ltd").status == "idle"
    assert _find(root, "Hardware").status == "idle"
    assert tree(s, w.id).status == "running"
    emit(s, w.id, "completed", {"job_id": 1})
    assert tree(s).status == "completed"


def test_failed_child_does_not_fail_siblings(seeded):
    s = seeded
    wf, ws = _worker(s, "Alpha Ltd"), _worker(s, "Alpha Ltd", "synthesis")
    emit(s, wf.id, "failed", {"job_id": 1, "error": "boom"})
    emit(s, ws.id, "completed", {"job_id": 2})
    root = tree(s)
    alpha = _find(root, "Alpha Ltd")
    by_id = {c.id: c for c in alpha.children}
    assert by_id[wf.id].status == "failed"
    assert by_id[ws.id].status == "completed"
    assert alpha.status == "degraded"
    assert _find(root, "Beta Ltd").status == "idle"
    assert root.status == "degraded"


def test_degraded_not_failed_for_ancestors_but_running_wins(seeded):
    s = seeded
    emit(s, _worker(s, "Alpha Ltd").id, "failed", {})
    emit(s, _worker(s, "Beta Ltd").id, "started", {"job_id": 5})
    root = tree(s)
    assert _find(root, "Alpha Ltd").status == "degraded"
    assert _find(root, "Beta Ltd").status == "running"
    assert _find(root, "IT Services").status == "running"   # running outranks degraded
    assert _find(root, "Hardware").status == "idle"


def test_own_failed_stays_failed_and_queued_status(seeded):
    s = seeded
    c = _company(s, "Delta Ltd")
    emit(s, c.id, "failed", {})
    emit(s, _worker(s, "Gamma Ltd").id, "queued", {"job_id": 3})
    root = tree(s)
    assert _find(root, "Delta Ltd").status == "failed"
    assert _find(root, "Devices").status == "degraded"
    assert _find(root, "Gamma Ltd").status == "queued"
    assert _find(root, "Products").status == "queued"


def test_current_task_cleared_after_completion(seeded):
    s = seeded
    w = _worker(s, "Alpha Ltd")
    emit(s, w.id, "started", {"job_id": 7, "task": "read filings"})
    n = tree(s, w.id)
    assert n.current_task == "read filings" and n.status == "running"
    emit(s, w.id, "step", {"note": "x"})
    assert tree(s, w.id).current_task == "read filings"
    emit(s, w.id, "completed", {"job_id": 7})
    n = tree(s, w.id)
    assert n.current_task is None and n.status == "completed" and n.last_run is not None
    emit(s, w.id, "started", {"job_id": 8})
    assert tree(s, w.id).current_task == "8"          # falls back to job id
    emit(s, w.id, "failed", {"job_id": 8})
    assert tree(s, w.id).current_task is None


def test_last_run_is_latest_completed_or_failed_including_descendants(seeded):
    s = seeded
    assert tree(s).last_run is None
    w1, w2 = _worker(s, "Alpha Ltd"), _worker(s, "Beta Ltd")
    e1 = emit(s, w1.id, "completed", {})
    e2 = emit(s, w2.id, "failed", {})
    s.refresh(e1), s.refresh(e2)
    root = tree(s)
    assert root.last_run == max(e1.created_at, e2.created_at)
    assert tree(s, w1.id).last_run == e1.created_at


def test_cost_rolls_up_to_parents(seeded):
    s = seeded
    emit(s, _worker(s, "Alpha Ltd").id, "output", {}, cost=1.5)
    emit(s, _worker(s, "Alpha Ltd", "synthesis").id, "output", {}, cost=0.25)
    emit(s, _company(s, "Alpha Ltd").id, "step", {}, cost=0.5)
    emit(s, _worker(s, "Beta Ltd").id, "output", {}, cost=2.0)
    root = tree(s)
    assert _find(root, "Alpha Ltd").cost_today == pytest.approx(2.25)
    assert _find(root, "Beta Ltd").cost_today == pytest.approx(2.0)
    assert _find(root, "IT Services").cost_today == pytest.approx(4.25)
    assert _find(root, "Software").cost_today == pytest.approx(4.25)
    assert _find(root, "Hardware").cost_today == 0
    assert root.cost_today == pytest.approx(4.25)


def test_cost_only_counts_today_utc(seeded):
    s = seeded
    w = _worker(s, "Alpha Ltd")
    midnight = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    for when, cost in ((midnight - timedelta(seconds=1), 10.0), (midnight - timedelta(days=3), 5.0),
                       (midnight, 1.0)):
        s.add(AgentEvent(agent_id=w.id, kind="output", payload={}, cost=cost, created_at=when))
    s.flush()
    assert tree(s, w.id, now=midnight + timedelta(hours=5)).cost_today == pytest.approx(1.0)
    assert tree(s).cost_today >= 0


def test_unknown_root_raises(seeded):
    with pytest.raises(ValueError):
        tree(seeded, 10**9)


def test_tree_empty_registry_raises(session):
    with pytest.raises(ValueError):
        tree(session)


def test_tree_query_count_bounded(session):
    for i in range(60):
        _inst(session, f"Co {i:03d}", f"Sec{i % 5}", f"Ind{i % 12}")
    seed_agents(session, MACRO)
    assert session.query(Agent).count() > 200
    emit(session, _company(session, "Co 001").id, "started", {"job_id": 1})
    count = []

    def counter(conn, cursor, statement, *a):
        count.append(statement)

    event.listen(session.get_bind(), "before_cursor_execute", counter)
    try:
        root = tree(session)
        n_small = len(count)
        assert root.status == "running"
    finally:
        event.remove(session.get_bind(), "before_cursor_execute", counter)
    assert n_small <= 6, count
    assert len(list(_walk(root))) > 200


# --- seeding ---------------------------------------------------------------------------

def test_seed_agents_idempotent(session):
    _small_universe(session)
    created = seed_agents(session, MACRO)
    assert created == 1 + 1 + 2 + 3 + 4 + 8
    total = session.query(Agent).count()
    assert seed_agents(session, MACRO) == 0
    assert session.query(Agent).count() == total
    _inst(session, "Epsilon Ltd", "Hardware", "Devices")
    assert seed_agents(session, MACRO) == 3          # company + 2 workers
    assert session.query(Agent).count() == total + 3


def test_seed_second_macro_sector_shares_master(session):
    _small_universe(session)
    seed_agents(session, MACRO)
    seed_agents(session, "Financials")
    assert session.query(Agent).filter_by(tier=tier_number("master")).count() == 1
    assert session.query(Agent).filter_by(tier=tier_number("macro_sector")).count() == 2


# --- emit ------------------------------------------------------------------------------

def test_emit_validation(seeded):
    s = seeded
    aid = _company(s, "Alpha Ltd").id
    for kw in (dict(kind="bogus", payload={}),
               dict(kind="step", payload={}, cost=-0.1),
               dict(kind="step", payload={}, cost=float("nan")),
               dict(kind="step", payload={}, cost=float("inf"))):
        with pytest.raises(ValueError):
            emit(s, aid, **kw)
    with pytest.raises(ValueError):
        emit(s, 10**9, "step", {})
    assert s.query(AgentEvent).count() == 0


def test_emit_returns_flushed_event_and_all_kinds(seeded):
    s = seeded
    aid = _company(s, "Alpha Ltd").id
    for kind in ("queued", "started", "step", "output", "failed", "completed"):
        ev = emit(s, aid, kind, {"k": kind}, cost=0.1)
        assert ev.id is not None and ev.kind == kind and ev.cost == 0.1


def test_enqueue_and_release_emit_queued_events(seeded):
    s = seeded
    aid = _company(s, "Alpha Ltd").id
    job = enqueue(s, aid, "manual")
    [ev] = s.query(AgentEvent).filter_by(agent_id=aid).all()
    assert ev.kind == "queued" and ev.payload == {"job_id": job.id, "trigger": "manual"}
    release(s, job)
    evs = s.query(AgentEvent).filter_by(agent_id=aid).order_by(AgentEvent.id).all()
    assert [e.kind for e in evs] == ["queued", "queued"]
    assert evs[1].payload["reason"] == "budget_halt" and evs[1].payload["job_id"] == job.id


def test_run_id_in_payload(seeded):
    s = seeded
    ev = emit(s, _company(s, "Alpha Ltd").id, "step", {"run_id": "r1", "a": 1})
    assert ev.payload["run_id"] == "r1"


# --- append-only -----------------------------------------------------------------------

def test_events_are_append_only(factory):
    with factory() as s:
        s.add(Agent(tier=1, role="master"))
        s.flush()
        emit(s, s.query(Agent).one().id, "step", {"a": 1})
        s.commit()
    with factory() as s:                      # ORM update
        s.query(AgentEvent).one().kind = "output"
        with pytest.raises(AppendOnlyError, match="append-only"):
            s.flush()
        s.rollback()
    with factory() as s:                      # ORM delete
        s.delete(s.query(AgentEvent).one())
        with pytest.raises(AppendOnlyError, match="append-only"):
            s.flush()
        s.rollback()
    for sql in ("UPDATE agent_events SET kind = 'output'", "DELETE FROM agent_events"):
        with factory() as s:                  # raw SQL, bypassing the ORM listeners
            with pytest.raises(DBAPIError, match="append-only"):
                s.execute(text(sql))
            s.rollback()
    with factory() as s:
        [ev] = s.query(AgentEvent).all()
        assert ev.kind == "step" and ev.payload == {"a": 1}


# --- single write path -----------------------------------------------------------------

def test_only_events_module_constructs_agent_event():
    src = ROOT / "src"
    offenders = []
    for p in src.rglob("*.py"):
        if p.name == "events.py" and p.parent.name == "runtime":
            continue
        if re.search(r"(?<!class )\bAgentEvent\s*\(", p.read_text()):
            offenders.append(str(p))
    assert offenders == []


# --- migration 0007 --------------------------------------------------------------------

def test_migration_0007_up_down():
    assert_safe_test_db(TEST_URL)
    base = TEST_URL.rsplit("/", 1)[0]
    scratch_url = f"{base}/ier_migration_test"
    admin = create_engine(f"{base}/postgres", isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text("DROP DATABASE IF EXISTS ier_migration_test WITH (FORCE)"))
        c.execute(text("CREATE DATABASE ier_migration_test"))
    eng = create_engine(scratch_url)
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    cfg.set_main_option("prepend_sys_path", str(ROOT / "src"))
    previous = os.environ.get("IER_DATABASE_URL")
    os.environ["IER_DATABASE_URL"] = scratch_url

    def trigger_count():
        with eng.connect() as c:
            return c.execute(text(
                "SELECT count(*) FROM pg_trigger WHERE tgrelid = 'agent_events'::regclass "
                "AND NOT tgisinternal")).scalar()
    try:
        command.upgrade(cfg, "0006")
        assert trigger_count() == 0
        command.upgrade(cfg, "0007")
        assert trigger_count() >= 1
        with eng.begin() as c:
            c.execute(text("INSERT INTO agents (tier, role) VALUES (1, 'master')"))
            c.execute(text("INSERT INTO agent_events (agent_id, kind) VALUES (1, 'step')"))
        with pytest.raises(DBAPIError, match="append-only"):
            with eng.begin() as c:
                c.execute(text("DELETE FROM agent_events"))
        command.downgrade(cfg, "0006")
        assert trigger_count() == 0
        with eng.begin() as c:
            c.execute(text("DELETE FROM agent_events"))
        command.upgrade(cfg, "head")
        assert trigger_count() >= 1
    finally:
        if previous is None:
            os.environ.pop("IER_DATABASE_URL", None)
        else:
            os.environ["IER_DATABASE_URL"] = previous
        eng.dispose()
        with admin.connect() as c:
            c.execute(text("DROP DATABASE IF EXISTS ier_migration_test WITH (FORCE)"))
        admin.dispose()
