"""Company agent graph, execution order and run planning (Task 13). Agents are stubbed by
monkeypatching the module-level callables the graph uses; no model is ever called."""
import threading
from datetime import date

import pytest
from sqlalchemy import select

from ier.agents.sector import SectorItem, SectorView
from ier.agents.synthesis import Claim
from ier.agents.verifier import VerifiedClaims
from ier.db.models import Agent, AgentEvent, Document, Fact, Instrument, Job, RunLog, Scorecard, Source
from ier.graph import company as cg
from ier.llm.router import BudgetExceeded, load_config as load_llm_config
from ier.rules.engine import score_value
from ier.runtime.events import emit, seed_agents
from ier.runtime.queue import DONE, claim_next, complete
from ier.runtime.worker import run_workers
from ier.tools.exchange_filings import FilingRef
from tests.runtime.conftest import wait_for

MACRO = "Information Technology"
OLD = date(2020, 1, 1)
_n = 0


def _inst(session, name, sector="Software", industry="IT Services"):
    global _n
    _n += 1
    i = Instrument(isin=f"INE{_n:09d}", name=name, macro_sector=MACRO, sector=sector,
                   industry=industry, status="active", listing_date=OLD, avg_daily_value=5e7)
    session.add(i)
    session.flush()
    return i


def _source(session, inst, content_hash="h1"):
    src = Source(url=f"http://example.test/{inst.id}/{content_hash}", type="filing", tier=1,
                 content_hash=content_hash)
    session.add(src)
    session.flush()
    session.add(Document(source_id=src.id, instrument_id=inst.id, title="results"))
    session.flush()
    return src


def _company(session, inst):
    return session.scalars(select(Agent).where(
        Agent.scope_instrument_id == inst.id, Agent.role == "company")).one()


def _child(session, parent_id, role):
    return session.scalars(select(Agent).where(Agent.parent_id == parent_id, Agent.role == role)).one()


def _events(session, agent_id, kinds=None):
    q = select(AgentEvent).where(AgentEvent.agent_id == agent_id).order_by(AgentEvent.id)
    if kinds:
        q = q.where(AgentEvent.kind.in_(kinds))
    return list(session.scalars(q))


@pytest.fixture
def world(session):
    a = _inst(session, "Alpha Ltd")
    b = _inst(session, "Beta Ltd")
    _source(session, a)
    _source(session, b)
    seed_agents(session, MACRO)
    return session, a, b


@pytest.fixture
def stubs(monkeypatch):
    """Stub every agent callable the graph uses. `calls` records (node, instrument_id)."""
    calls = []
    behaviour = {}

    def maybe_raise(node, iid):
        exc = behaviour.get((node, iid)) or behaviour.get((node, None))
        if exc is not None:
            raise exc

    def fake_extract(session, instrument_id, run_id, **kw):
        calls.append(("fundamentals", instrument_id))
        session.add(RunLog(run_id=run_id, role="extract", prompt_tokens=10, completion_tokens=5))
        session.flush()
        maybe_raise("fundamentals", instrument_id)
        return []

    def fake_draft(session, instrument_id, rule_result, run_id, **kw):
        calls.append(("synthesis", instrument_id))
        maybe_raise("synthesis", instrument_id)
        return [Claim(kind="benefit", text="Revenue grew 12% year on year.", severity=3,
                      horizon="long", evidence_ids=[])]

    def fake_verify(claims, session, *, instrument_id=None, sector=None, run_id="verify", **kw):
        assert instrument_id is not None and sector is None  # company scope only
        calls.append(("verifier", instrument_id))
        maybe_raise("verifier", instrument_id)
        return VerifiedClaims(kept=list(claims))

    monkeypatch.setattr(cg, "extract_facts", fake_extract)
    monkeypatch.setattr(cg, "draft_claims", fake_draft)
    monkeypatch.setattr(cg, "verify", fake_verify)
    def fake_list_filings(isin, *, session, **kw):
        exc = behaviour.get("listing_error")
        if exc is not None:
            raise exc
        return list(behaviour.get("listing", []))

    monkeypatch.setattr(cg, "list_filings", fake_list_filings)
    monkeypatch.setattr(cg, "SIGNAL_HOOK", None)
    return calls, behaviour


# --- the five named tests -----------------------------------------------------------------

def test_company_job_waits_for_sector_and_industry_jobs(world):
    s, a, b = world
    jobs = cg.plan_run(s, MACRO, "run-1")
    roles = {j.id: s.get(Agent, j.agent_id).role for j in jobs}
    assert sorted(roles.values()) == ["company", "company", "industry", "sector"]
    assert all(j.payload == {"run_id": "run-1"} for j in jobs)
    company_jobs = [j for j in jobs if roles[j.id] == "company"]
    industry_job = next(j for j in jobs if roles[j.id] == "industry")
    sector_job = next(j for j in jobs if roles[j.id] == "sector")
    assert industry_job.depends_on == sector_job.id
    assert all(j.depends_on == industry_job.id for j in company_jobs)

    assert claim_next(s, "company") is None
    assert claim_next(s, "industry") is None  # waits for the sector job
    sj = claim_next(s, "sector")
    assert sj.id == sector_job.id
    assert claim_next(s, "company") is None  # sector running, industry not done
    complete(s, sj)
    assert claim_next(s, "company") is None  # sector done, industry still queued
    ij = claim_next(s, "industry")
    assert ij.id == industry_job.id
    assert claim_next(s, "company") is None
    complete(s, ij)
    claimed = {claim_next(s, "company").id, claim_next(s, "company").id}
    assert claimed == {j.id for j in company_jobs}


def test_each_worker_emits_started_step_output_completed(world, stubs):
    s, a, _ = world
    comp = _company(s, a)
    fund, synth = _child(s, comp.id, "fundamentals"), _child(s, comp.id, "synthesis")
    res = cg.run_company_agent(s, comp.id, "run-ev")
    assert res.status == "completed" and res.scorecard_id is not None and res.error is None
    for worker in (fund, synth):
        kinds = [e.kind for e in _events(s, worker.id)]
        assert kinds[0] == "started" and kinds[-1] == "completed"
        assert "step" in kinds and "output" in kinds
        assert kinds.index("output") < len(kinds) - 1
        assert all(e.payload.get("run_id") == "run-ev" for e in _events(s, worker.id))
    company_kinds = [e.kind for e in _events(s, comp.id)]
    assert company_kinds[0] == "started" and company_kinds[-1] == "completed"
    # one step per node boundary, on the responsible agent; cost from RunLog usage
    steps = [e for aid in (fund.id, synth.id, comp.id) for e in _events(s, aid, ["step"])]
    assert sorted(e.payload["node"] for e in steps) == sorted(
        ["fundamentals", "rules", "synthesis", "verifier", "scorecard", "signal"])
    fstep = next(e for e in steps if e.payload["node"] == "fundamentals")
    assert fstep.agent_id == fund.id and fstep.cost == 15
    assert {e.payload["node"] for e in steps if e.agent_id == synth.id} == {
        "synthesis", "verifier", "scorecard"}
    signal = next(e for e in steps if e.payload["node"] == "signal")
    assert signal.agent_id == comp.id and "signal skipped: not configured" in signal.payload["message"]
    out = _events(s, synth.id, ["output"])[0].payload
    assert out["scorecard_id"] == res.scorecard_id


def test_unchanged_inputs_skipped_with_event(world, stubs):
    s, a, _ = world
    calls, _ = stubs
    comp = _company(s, a)
    assert cg.run_company_agent(s, comp.id, "run-1").status == "completed"
    n = len(calls)
    res = cg.run_company_agent(s, comp.id, "run-2")
    assert res.status == "skipped" and res.scorecard_id is None
    assert len(calls) == n  # no agent (and so no model) was called
    outs = [e.payload for e in _events(s, comp.id, ["output"])]
    assert outs[-1]["skipped"] == "unchanged" and outs[-1]["run_id"] == "run-2"
    # a changed source content hash is a changed input
    src = s.scalars(select(Source).join(Document, Document.source_id == Source.id)
                    .where(Document.instrument_id == a.id)).one()
    src.content_hash = "h2"
    s.flush()
    assert cg.run_company_agent(s, comp.id, "run-3").status == "completed"
    assert len(calls) > n
    # a changed sector tailwind is a changed input too
    sector = s.get(Agent, s.get(Agent, comp.parent_id).parent_id)
    emit(s, sector.id, "output", {"kind": "sector_view", "tailwind_input": 70.0, "run_id": "x"})
    assert cg.run_company_agent(s, comp.id, "run-4").status == "completed"
    assert cg.run_company_agent(s, comp.id, "run-5").status == "skipped"


def test_worker_failure_emits_failed_and_run_continues_for_other_workers(world, stubs):
    s, a, b = world
    calls, behaviour = stubs
    behaviour[("synthesis", a.id)] = RuntimeError("model fell over")
    ca, cb = _company(s, a), _company(s, b)
    synth_a, fund_a = _child(s, ca.id, "synthesis"), _child(s, ca.id, "fundamentals")
    ra = cg.run_company_agent(s, ca.id, "run-f")
    assert ra.status == "failed" and ra.scorecard_id is None and "model fell over" in ra.error
    assert [e.kind for e in _events(s, synth_a.id)] == ["started", "failed"]
    failed = _events(s, synth_a.id, ["failed"])[0].payload
    assert failed["node"] == "synthesis" and "model fell over" in failed["error"]
    assert _events(s, ca.id)[-1].kind == "failed"
    assert _events(s, fund_a.id)[-1].kind == "completed"  # the earlier worker finished
    assert ("verifier", a.id) not in calls  # later nodes did not run
    assert s.scalars(select(Scorecard).where(Scorecard.instrument_id == a.id)).first() is None
    log = s.scalars(select(RunLog).where(RunLog.agent_id == ca.id)).one()
    assert log.outputs["status"] == "failed"
    # the other company is unaffected
    rb = cg.run_company_agent(s, cb.id, "run-f")
    assert rb.status == "completed" and rb.scorecard_id is not None
    # a failed run is not a "last completed" run: same inputs run again, not skipped
    behaviour.clear()
    assert cg.run_company_agent(s, ca.id, "run-g").status == "completed"


def test_run_log_records_inputs_and_model_version(world, stubs):
    s, a, _ = world
    comp = _company(s, a)
    cg.run_company_agent(s, comp.id, "run-log")
    log = s.scalars(select(RunLog).where(RunLog.agent_id == comp.id)).one()
    assert log.run_id == "run-log" and log.role == "company"
    assert log.inputs["instrument_id"] == a.id and log.inputs["agent_id"] == comp.id
    assert isinstance(log.inputs["fingerprint"], str) and len(log.inputs["fingerprint"]) == 64
    models = load_llm_config()["models"]
    assert models["extract"] in log.model_version and models["synthesise"] in log.model_version
    assert log.outputs["status"] == "completed" and log.outputs["scorecard_id"] is not None


# --- further behaviour ----------------------------------------------------------------------

def test_plan_run_is_idempotent_per_run_id(world):
    s, _, _ = world
    first = cg.plan_run(s, MACRO, "run-1")
    again = cg.plan_run(s, MACRO, "run-1")
    assert [j.id for j in again] == [j.id for j in first]
    assert s.query(Job).count() == 4
    other = cg.plan_run(s, MACRO, "run-2")
    assert s.query(Job).count() == 8 and not {j.id for j in other} & {j.id for j in first}


def test_plan_run_unknown_macro_sector_raises(world):
    s, _, _ = world
    with pytest.raises(ValueError):
        cg.plan_run(s, "Nope", "run-1")


def test_budget_exceeded_propagates(world, stubs):
    s, a, _ = world
    _, behaviour = stubs
    behaviour[("fundamentals", a.id)] = BudgetExceeded("per_run cap")
    with pytest.raises(BudgetExceeded):
        cg.run_company_agent(s, _company(s, a).id, "run-b")


def test_signal_hook_called_when_set(world, stubs, monkeypatch):
    s, a, _ = world
    seen = []
    monkeypatch.setattr(cg, "SIGNAL_HOOK", lambda session, iid, sid: seen.append((iid, sid)))
    res = cg.run_company_agent(s, _company(s, a).id, "run-s")
    assert seen == [(a.id, res.scorecard_id)]


def test_default_hook_persists_signal_end_to_end(world, stubs, monkeypatch):
    from ier.db.models import Signal
    s, a, _ = world
    monkeypatch.setattr(cg, "SIGNAL_HOOK", None)
    cg.register_default_handlers({})
    assert cg.SIGNAL_HOOK is not None
    res = cg.run_company_agent(s, _company(s, a).id, "run-e2e")
    assert res.status == "completed"
    sig = s.scalars(select(Signal).where(Signal.scorecard_id == res.scorecard_id)).one()
    assert sig.instrument_id == a.id and sig.reasons
    assert sig.rating in {"accumulate", "hold", "reduce", "avoid", "insufficient evidence"}
    card = s.get(Scorecard, res.scorecard_id)
    assert card.rules_version


def test_register_default_handlers_keeps_existing_hook(stubs, monkeypatch):
    def mine(session, iid, sid):
        pass

    monkeypatch.setattr(cg, "SIGNAL_HOOK", mine)
    cg.register_default_handlers({})
    assert cg.SIGNAL_HOOK is mine


def test_signal_hook_failure_fails_company_only(world, stubs, monkeypatch):
    s, a, _ = world

    def boom(session, iid, sid):
        raise RuntimeError("signal broke")

    monkeypatch.setattr(cg, "SIGNAL_HOOK", boom)
    comp = _company(s, a)
    res = cg.run_company_agent(s, comp.id, "run-s")
    assert res.status == "failed" and "signal broke" in res.error
    assert _events(s, comp.id)[-1].kind == "failed"


def test_missing_worker_agent_fails_clearly(session, stubs):
    a = _inst(session, "Solo Ltd")
    seed_agents(session, MACRO)
    comp = _company(session, a)
    synth = _child(session, comp.id, "synthesis")
    session.delete(synth)
    session.flush()
    res = cg.run_company_agent(session, comp.id, "run-m")
    assert res.status == "failed" and "synthesis" in res.error
    assert stubs[0] == []
    log = session.scalars(select(RunLog).where(RunLog.agent_id == comp.id)).one()
    assert log.run_id == "run-m" and log.outputs["status"] == "failed"
    assert "synthesis" in log.outputs["error"] and log.model_version


def test_sector_tailwind_reaches_rules_and_fact_takes_precedence(world, stubs):
    s, a, b = world
    ca, cb = _company(s, a), _company(s, b)
    sector = s.get(Agent, s.get(Agent, ca.parent_id).parent_id)
    emit(s, sector.id, "output", {"kind": "sector_view", "tailwind_input": 80.0, "run_id": "r"})
    ra = cg.run_company_agent(s, ca.id, "run-t")
    card = s.get(Scorecard, ra.scorecard_id)
    assert card.sub_scores["sector_tailwind"] == pytest.approx(score_value(80.0, 50, "higher_better"))
    # a Fact for the same metric wins over the sector context
    s.add(Fact(instrument_id=b.id, source_id=s.scalars(select(Source)).first().id,
               metric="sector_tailwind", value=30.0, period="FY26"))
    s.flush()
    rb = cg.run_company_agent(s, cb.id, "run-t")
    assert s.get(Scorecard, rb.scorecard_id).sub_scores["sector_tailwind"] == pytest.approx(
        score_value(30.0, 50, "higher_better"))


def test_no_sector_view_leaves_tailwind_none(world, stubs):
    s, a, _ = world
    res = cg.run_company_agent(s, _company(s, a).id, "run-n")
    assert s.get(Scorecard, res.scorecard_id).sub_scores["sector_tailwind"] is None


def test_sector_and_industry_handlers_store_output_events(world, monkeypatch):
    s, a, _ = world
    view = SectorView(sector="Software", drivers=(SectorItem("driver", "Demand rose.", 2, 1, (7,)),),
                      tailwind_input=70.0)
    monkeypatch.setattr(cg, "analyse_sector", lambda session, sector, run_id, **kw: view)
    monkeypatch.setattr(cg, "rank_industry", lambda session, industry: [(a.id, 61.0)])
    jobs = cg.plan_run(s, MACRO, "run-h")
    sector_job = next(j for j in jobs if s.get(Agent, j.agent_id).role == "sector")
    industry_job = next(j for j in jobs if s.get(Agent, j.agent_id).role == "industry")
    cg.run_sector_agent(s, sector_job)
    cg.run_industry_agent(s, industry_job)
    sv = _events(s, sector_job.agent_id, ["output"])[-1].payload
    assert sv["kind"] == "sector_view" and sv["tailwind_input"] == 70.0 and sv["run_id"] == "run-h"
    assert sv["drivers"][0]["text"] == "Demand rose."
    iv = _events(s, industry_job.agent_id, ["output"])[-1].payload
    assert iv["kind"] == "industry_ranking" and iv["ranking"] == [[a.id, 61.0]]
    assert cg.latest_sector_tailwind(s, _company(s, a).id) == 70.0


def test_register_default_handlers(monkeypatch):
    monkeypatch.setattr(cg, "SIGNAL_HOOK", None)
    reg = {}
    cg.register_default_handlers(reg)
    assert set(reg) == {"company", "sector", "industry", "source", "segment"}


def test_end_to_end_through_run_workers_in_dependency_order(factory, monkeypatch, stubs):
    calls, _ = stubs
    order = []
    lock = threading.Lock()
    with factory() as s:
        a = _inst(s, "Gamma Ltd")
        _source(s, a)
        seed_agents(s, MACRO)
        cg.plan_run(s, MACRO, "run-e2e")
        s.commit()
        comp_id = _company(s, a).id

    def fake_sector(session, sector, run_id, **kw):
        with lock:
            order.append("sector")
        return SectorView(sector=sector, tailwind_input=90.0)

    def fake_rank(session, industry):
        with lock:
            order.append("industry")
        return []

    monkeypatch.setattr(cg, "analyse_sector", fake_sector)
    monkeypatch.setattr(cg, "rank_industry", fake_rank)
    handlers = {}
    cg.register_default_handlers(handlers)
    stop = threading.Event()
    t = threading.Thread(target=run_workers, args=({"sector": 1, "industry": 1, "company": 1}, stop),
                         kwargs=dict(session_factory=factory, handlers=handlers, poll_interval=0.01),
                         daemon=True)
    t.start()

    def all_done():
        with factory() as s:
            return all(j.status == DONE for j in s.query(Job))
    try:
        assert wait_for(all_done, timeout=20)
    finally:
        stop.set()
        t.join(10)
    assert order == ["sector", "industry"]
    assert ("fundamentals", a.id) in calls
    with factory() as s:
        jobs = {s.get(Agent, j.agent_id).role: j for j in s.query(Job)}
        kinds = [e.kind for e in _events(s, comp_id)]
        assert kinds.count("started") == 1 and kinds[-1] == "completed"
        card = s.scalars(select(Scorecard).where(Scorecard.instrument_id == a.id)).one()
        assert card.sub_scores["sector_tailwind"] == pytest.approx(90.0)
        # completion order follows dependencies (event ids are commit-ordered per job here)
        done_ids = {role: max(e.id for e in _events(s, j.agent_id, ["completed"]))
                    for role, j in jobs.items()}
        assert done_ids["sector"] < done_ids["industry"] < done_ids["company"]


def test_company_handler_failure_marks_job_for_retry(factory, monkeypatch, stubs):
    _, behaviour = stubs
    with factory() as s:
        a = _inst(s, "Delta Ltd")
        _source(s, a)
        seed_agents(s, MACRO)
        comp = _company(s, a)
        job = Job(agent_id=comp.id, trigger="manual", status="queued", payload={"run_id": "r1"})
        s.add(job)
        s.commit()
        comp_id, synth_id, job_id = comp.id, _child(s, comp.id, "synthesis").id, job.id
    behaviour[("synthesis", None)] = RuntimeError("nope")
    handlers = {}
    cg.register_default_handlers(handlers)
    stop = threading.Event()
    t = threading.Thread(target=run_workers, args=({"company": 1}, stop),
                         kwargs=dict(session_factory=factory, handlers=handlers, poll_interval=0.01,
                                     config={"retry": {"max_attempts": 1, "backoff_base_seconds": 0}}),
                         daemon=True)
    t.start()

    def failed():
        with factory() as s:
            return s.get(Job, job_id).status == "failed"
    try:
        assert wait_for(failed, timeout=20)
    finally:
        stop.set()
        t.join(10)
    with factory() as s:
        assert [e.kind for e in _events(s, synth_id)] == ["started", "failed"]  # kept, committed
        assert _events(s, comp_id)[-1].kind == "failed"


def test_failing_node_changes_rolled_back(world, stubs):
    s, a, _ = world
    _, behaviour = stubs
    behaviour[("fundamentals", a.id)] = RuntimeError("fetch broke")
    comp = _company(s, a)
    res = cg.run_company_agent(s, comp.id, "run-rb")
    assert res.status == "failed" and res.error.startswith("fundamentals:")
    # the stub wrote a RunLog row inside the node before raising: rolled back with the savepoint
    assert s.scalars(select(RunLog).where(RunLog.run_id == "run-rb", RunLog.role == "extract")).first() is None
    fund = _child(s, comp.id, "fundamentals")
    assert [e.kind for e in _events(s, fund.id)] == ["started", "failed"]
    assert _events(s, _child(s, comp.id, "synthesis").id) == []


# --- fix round 1: fingerprint covers listed filings, facts, rounded tailwind ------------------

def _ref(isin, n, pub=date(2026, 10, 1)):
    return FilingRef(isin=isin, url=f"https://filings.invalid/f/{n}.txt", filing_type="results",
                     period=f"Q{n}FY27", published_date=pub)


def test_new_listed_filing_not_yet_ingested_is_not_skipped(world, stubs):
    s, a, _ = world
    calls, behaviour = stubs
    comp = _company(s, a)
    behaviour["listing"] = [_ref(a.isin, 1)]
    assert cg.run_company_agent(s, comp.id, "r1").status == "completed"
    assert cg.run_company_agent(s, comp.id, "r2").status == "skipped"
    behaviour["listing"] = [_ref(a.isin, 2), _ref(a.isin, 1)]  # new filing, no Source yet
    n = len(calls)
    assert cg.run_company_agent(s, comp.id, "r3").status == "completed"
    assert ("fundamentals", a.id) in calls[n:]


def test_same_listing_sources_and_facts_skipped(world, stubs):
    s, a, _ = world
    _, behaviour = stubs
    comp = _company(s, a)
    behaviour["listing"] = [_ref(a.isin, 1), _ref(a.isin, 2)]
    assert cg.run_company_agent(s, comp.id, "r1").status == "completed"
    behaviour["listing"] = [_ref(a.isin, 2), _ref(a.isin, 1)]  # order does not matter
    assert cg.run_company_agent(s, comp.id, "r2").status == "skipped"
    assert cg.run_company_agent(s, comp.id, "r3", force=True).status == "completed"


def test_listing_failure_is_not_skipped(world, stubs):
    s, a, _ = world
    _, behaviour = stubs
    comp = _company(s, a)
    assert cg.run_company_agent(s, comp.id, "r1").status == "completed"
    behaviour["listing_error"] = RuntimeError("index down")
    assert cg.run_company_agent(s, comp.id, "r2").status == "completed"
    log = s.scalars(select(RunLog).where(RunLog.run_id == "r2", RunLog.role == "company")).one()
    assert log.inputs["fingerprint"] is None
    # a run whose listing failed leaves no fingerprint to skip against either
    del behaviour["listing_error"]
    assert cg.run_company_agent(s, comp.id, "r3").status == "completed"
    assert cg.run_company_agent(s, comp.id, "r4").status == "skipped"


def test_stale_index_fetch_counts_as_listing_failure(world, stubs, monkeypatch):
    s, a, _ = world
    comp = _company(s, a)
    url = "https://filings.invalid/index/{isin}.json"
    cfg = {"index_url": url}
    assert cg.run_company_agent(s, comp.id, "r1", filings_config=cfg).status == "completed"
    assert cg.run_company_agent(s, comp.id, "r2", filings_config=cfg).status == "skipped"
    s.add(Source(url=url.format(isin=a.isin), type="http", tier=1, status="stale"))
    s.flush()
    assert cg.run_company_agent(s, comp.id, "r3", filings_config=cfg).status == "completed"


def test_tailwind_noise_within_rounding_still_skipped(world, stubs):
    s, a, _ = world
    comp = _company(s, a)
    sector = s.get(Agent, s.get(Agent, comp.parent_id).parent_id)
    emit(s, sector.id, "output", {"kind": "sector_view", "tailwind_input": 70.2, "run_id": "x"})
    assert cg.run_company_agent(s, comp.id, "r1").status == "completed"
    emit(s, sector.id, "output", {"kind": "sector_view", "tailwind_input": 69.8, "run_id": "y"})
    assert cg.run_company_agent(s, comp.id, "r2").status == "skipped"
    emit(s, sector.id, "output", {"kind": "sector_view", "tailwind_input": 72.0, "run_id": "z"})
    assert cg.run_company_agent(s, comp.id, "r3").status == "completed"


def test_new_fact_is_not_skipped(world, stubs):
    s, a, _ = world
    comp = _company(s, a)
    assert cg.run_company_agent(s, comp.id, "r1").status == "completed"
    assert cg.run_company_agent(s, comp.id, "r2").status == "skipped"
    s.add(Fact(instrument_id=a.id, source_id=s.scalars(select(Source)).first().id,
               metric="operating_margin_industry_median", value=20.0, period="FY26"))
    s.flush()
    assert cg.run_company_agent(s, comp.id, "r3").status == "completed"


def test_facts_written_by_the_run_itself_do_not_defeat_the_next_skip(world, stubs, monkeypatch):
    s, a, _ = world
    comp = _company(s, a)
    src_id = s.scalars(select(Source)).first().id

    def extract_writing_facts(session, instrument_id, run_id, **kw):
        if not session.scalars(select(Fact).where(Fact.instrument_id == instrument_id)).first():
            session.add(Fact(instrument_id=instrument_id, source_id=src_id,
                             metric="revenue_growth", value=12.0, period="FY26"))
            session.flush()
        return []

    monkeypatch.setattr(cg, "extract_facts", extract_writing_facts)
    assert cg.run_company_agent(s, comp.id, "r1").status == "completed"
    assert cg.run_company_agent(s, comp.id, "r2").status == "skipped"


def test_default_started_worker_processes_company_job(factory, monkeypatch, stubs):
    from ier.runtime import worker as worker_mod
    calls, _ = stubs
    monkeypatch.setattr(worker_mod, "HANDLERS", {})
    with factory() as s:
        a = _inst(s, "Epsilon Ltd")
        _source(s, a)
        seed_agents(s, MACRO)
        comp = _company(s, a)
        job = Job(agent_id=comp.id, trigger="manual", status="queued", payload={"run_id": "r-def"})
        s.add(job)
        s.commit()
        job_id = job.id
    stop = threading.Event()
    t = threading.Thread(target=run_workers, args=({"company": 1}, stop),
                         kwargs=dict(session_factory=factory, poll_interval=0.01), daemon=True)
    t.start()

    def done():
        with factory() as s:
            return s.get(Job, job_id).status == DONE
    try:
        assert wait_for(done, timeout=20)
    finally:
        stop.set()
        t.join(10)
    assert set(worker_mod.HANDLERS) == {"company", "sector", "industry", "source", "segment"}
    assert ("fundamentals", a.id) in calls
    with factory() as s:
        assert s.scalars(select(Scorecard).where(Scorecard.instrument_id == a.id)).one()


def test_default_registration_keeps_existing_handlers(monkeypatch):
    monkeypatch.setattr(cg, "SIGNAL_HOOK", None)
    mine = object()
    reg = cg.register_default_handlers({"company": mine}, overwrite=False)
    assert reg["company"] is mine and set(reg) == {"company", "sector", "industry", "source", "segment"}


# --- F4: calibration is part of the run fingerprint; a manual rerun never skips -----------

def _skip_then_change(s, comp, change):
    assert cg.run_company_agent(s, comp.id, "f1").status == "completed"
    assert cg.run_company_agent(s, comp.id, "f2").status == "skipped"
    change()
    assert cg.run_company_agent(s, comp.id, "f3").status == "completed"


def test_changed_signals_yaml_is_not_skipped(world, stubs, monkeypatch, tmp_path):
    from ier.signals import compute
    s, a, _ = world
    p = tmp_path / "signals.yaml"
    p.write_bytes(compute.DEFAULT_PATH.read_bytes())
    monkeypatch.setattr(compute, "DEFAULT_PATH", p)
    _skip_then_change(s, _company(s, a), lambda: p.write_text(p.read_text() + "\n# recalibrated\n"))


def test_changed_synthesis_yaml_is_not_skipped(world, stubs, monkeypatch, tmp_path):
    from ier.agents import verifier
    s, a, _ = world
    p = tmp_path / "synthesis.yaml"
    p.write_bytes(verifier.CONFIG_PATH.read_bytes())
    monkeypatch.setattr(verifier, "CONFIG_PATH", p)
    _skip_then_change(s, _company(s, a), lambda: p.write_text(p.read_text() + "\n# recalibrated\n"))


def test_changed_model_names_are_not_skipped(world, stubs, monkeypatch):
    s, a, _ = world
    real = load_llm_config()

    def change():
        monkeypatch.setattr(cg, "load_llm_config",
                            lambda *x, **k: {**real, "models": {**real["models"], "extract": "other:7b"}})
    _skip_then_change(s, _company(s, a), change)


def test_scoring_code_version_bump_is_not_skipped(world, stubs, monkeypatch):
    from ier.rules import engine
    s, a, _ = world
    _skip_then_change(s, _company(s, a),
                      lambda: monkeypatch.setattr(engine, "SCORING_CODE_VERSION", "test-bump"))


def test_manual_rerun_forces_a_run_even_when_unchanged(world, stubs):
    from ier.runtime.triggers import on_manual
    s, a, _ = world
    comp = _company(s, a)
    assert cg.run_company_agent(s, comp.id, "r1").status == "completed"
    job = on_manual(s, comp.id)[0]
    assert job.payload == {"force": True}
    res = cg.run_company_agent(s, comp.id, cg._job_run_id(job),
                               force=bool((job.payload or {}).get("force")), emit_lifecycle=False)
    assert res.status == "completed"
