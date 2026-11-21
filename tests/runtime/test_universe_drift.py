"""F6: universe drift. plan_run plans only instruments that pass build_universe now, and a
reclassified instrument keeps exactly one active company agent (re-parented, not duplicated)."""
from datetime import date

from sqlalchemy import func, select

from ier.db.models import Agent, Job
from ier.graph import company as cg
from ier.runtime.events import seed_agents
from ier.runtime.triggers import on_market_event
from tests.db.conftest import engine, session  # noqa: F401
from tests.graph.test_company import MACRO, _company, _inst, _source


def _companies(s, inst):
    return list(s.scalars(select(Agent).where(Agent.scope_instrument_id == inst.id,
                                              Agent.role == "company")))


def test_rule_plan_run_plans_only_instruments_in_the_universe_today(session):
    """Rule: a company job is planned only if its instrument passes build_universe at plan time
    (not suspended, no surveillance flag, liquid enough, listed long enough)."""
    s = session
    ok = _inst(s, "Still Fine Ltd")
    gone = _inst(s, "Later Suspended Ltd")
    flagged = _inst(s, "Later Flagged Ltd")
    illiquid = _inst(s, "Later Illiquid Ltd")
    for i in (ok, gone, flagged, illiquid):
        _source(s, i)
    seed_agents(s, MACRO)
    gone.status = "suspended"
    flagged.surveillance_flag = "ASM"
    illiquid.avg_daily_value = 1.0
    s.flush()
    jobs = cg.plan_run(s, MACRO, "r")
    planned = {j.agent_id for j in jobs}
    assert _company(s, ok).id in planned
    for i in (gone, flagged, illiquid):
        assert _company(s, i).id not in planned


def test_reclassified_instrument_keeps_one_company_agent_reparented(session):
    s = session
    a = _inst(s, "Moved Ltd")
    _source(s, a)
    seed_agents(s, MACRO)
    before = _companies(s, a)
    assert len(before) == 1
    a.industry = "IT Consulting"
    s.flush()
    seed_agents(s, MACRO)
    after = _companies(s, a)
    assert [c.id for c in after] == [before[0].id]          # same agent, history kept
    industry = s.get(Agent, after[0].parent_id)
    assert industry.role == "industry" and industry.scope_name == "IT Consulting"
    # workers stay under the one company agent
    assert s.scalar(select(func.count()).select_from(Agent).where(
        Agent.parent_id == after[0].id)) == 2
    jobs = cg.plan_run(s, MACRO, "r")
    assert sum(1 for j in jobs if j.agent_id == after[0].id) == 1


def test_pre_existing_duplicate_company_agents_are_retired(session):
    s = session
    a = _inst(s, "Dup Ltd")
    _source(s, a)
    seed_agents(s, MACRO)
    keep = _company(s, a)
    # what the old seed_agents produced after a reclassification: a second company agent
    dup = Agent(tier=keep.tier, parent_id=keep.parent_id, role="company", scope_instrument_id=a.id)
    s.add(dup)
    s.flush()
    seed_agents(s, MACRO)
    assert [c.id for c in _companies(s, a)] == [keep.id]
    s.refresh(dup)
    assert dup.parent_id is None and dup.role == "retired_company"
    jobs = cg.plan_run(s, MACRO, "r2")
    assert [j for j in jobs if j.agent_id == dup.id] == []
    assert {j.agent_id for j in on_market_event(s, a.id, "gap", 9.0, 5.0)} == {keep.id}
