"""F2: compute caps. The per-run cap is per company job (one document), a per-run cap hit fails
only that job, a per-day cap hit reschedules to the next UTC midnight, and model usage is
recorded even when the node or job that spent it rolls back."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from pydantic import BaseModel
from sqlalchemy import func, select

from ier.agents.sector import SectorView
from ier.db.models import Job, RunLog
from ier.graph import company as cg
from ier.llm import router
from ier.runtime.__main__ import run_until_drained
from ier.runtime.events import seed_agents
from tests.db.conftest import engine, session  # noqa: F401
from tests.graph.test_company import MACRO, _company, _inst, _source, stubs  # noqa: F401
from tests.runtime.conftest import factory  # noqa: F401

TIERS = {"sector": 1, "industry": 1, "company": 1}


class Out(BaseModel):
    answer: str


class Client:
    """openai.OpenAI stand-in: each call spends `tokens` (all prompt tokens)."""

    def __init__(self, content='{"answer": "x"}', tokens=60):
        self.content, self.tokens = content, tokens
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self.content))],
            usage=SimpleNamespace(prompt_tokens=self.tokens, completion_tokens=0))


def _cfg(per_run=100, per_day=10**9):
    return {"base_url": "http://unused/v1", "models": {"extract": "m", "synthesise": "m",
                                                      "reason": "m"},
            "caps": {"per_run": {"max_tokens": per_run, "max_model_seconds": 1e9},
                     "per_day": {"max_tokens": per_day, "max_model_seconds": 1e9}}}


def _world(factory, names=("A Ltd", "B Ltd", "C Ltd")):
    with factory() as s:
        insts = [_inst(s, n) for n in names]
        for i in insts:
            _source(s, i)
        seed_agents(s, MACRO)
        cg.plan_run(s, MACRO, "run-1")
        s.commit()
        return [i.id for i in insts]


def _handlers(monkeypatch, calls_for):
    """Company runs make `calls_for(instrument_id)` real router calls (fake model client)."""
    def extract(session, instrument_id, run_id, **kw):
        for _ in range(calls_for(instrument_id)):
            router.complete("extract", "p", Out, run_id, session=session, client=Client(),
                            config=CFG["cfg"])
        return []
    monkeypatch.setattr(cg, "extract_facts", extract)
    monkeypatch.setattr(cg, "analyse_sector", lambda s, sec, rid, **kw: SectorView(sector=sec))
    monkeypatch.setattr(cg, "rank_industry", lambda s, ind: [])
    h = {}
    cg.register_default_handlers(h)
    return h


CFG = {}


def _jobs(factory):
    with factory() as s:
        return s.execute(select(Job.id, Job.status, Job.payload, Job.attempts, Job.scheduled_at,
                                Job.agent_id).order_by(Job.id)).all()


def test_per_run_cap_is_per_company_job_and_does_not_poison_the_queue(factory, monkeypatch, stubs):
    # three companies x 60 tokens = 180 > per_run 100, but each company job spends only 60
    CFG["cfg"] = _cfg(per_run=100)
    _world(factory)
    h = _handlers(monkeypatch, lambda iid: 1)
    first = run_until_drained(factory, TIERS, handlers=h, timeout=20)
    assert first["timed_out"] is False and first["done"] == 5 and first["queued"] == 0
    with factory() as s:
        cg.plan_run(s, MACRO, "run-2")
        s.commit()
    second = run_until_drained(factory, TIERS, handlers=h, timeout=20)
    assert second["done"] == 10 and second["queued"] == 0 and second["failed"] == 0


def test_per_run_cap_hit_fails_only_that_job_without_halting(factory, monkeypatch, stubs):
    CFG["cfg"] = _cfg(per_run=100)
    a, b, c = _world(factory)
    h = _handlers(monkeypatch, lambda iid: 3 if iid == b else 1)   # B: 60, 120, then refused
    summary = run_until_drained(factory, TIERS, handlers=h, timeout=20,
                                config={"retry": {"max_attempts": 3, "backoff_base_seconds": 0}})
    assert summary["timed_out"] is False
    assert summary["failed"] == 1 and summary["done"] == 4 and summary["queued"] == 0
    with factory() as s:
        comp_b = _company(s, s.get(__import__("ier.db.models", fromlist=["Instrument"]).Instrument, b))
        job = s.scalars(select(Job).where(Job.agent_id == comp_b.id)).one()
        assert job.status == "failed" and "per_run" in job.last_error


def test_per_day_cap_reschedules_to_next_utc_midnight_and_halts(factory, monkeypatch, stubs):
    CFG["cfg"] = _cfg(per_run=10**9, per_day=100)
    _world(factory)
    h = _handlers(monkeypatch, lambda iid: 2)        # 60, 120: the 2nd call of job 1 is fine,
    summary = run_until_drained(factory, TIERS, handlers=h, timeout=20)   # job 2 is refused
    assert summary["timed_out"] is True              # halted with work left
    now = datetime.now(timezone.utc)
    midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    queued = [j for j in _jobs(factory) if j.status == "queued"]
    rescheduled = [j for j in queued if j.scheduled_at == midnight]
    assert rescheduled and all(j.attempts == 0 for j in rescheduled)


def test_usage_of_a_failed_node_is_kept_for_the_caps(factory, monkeypatch, stubs):
    # three invalid-JSON attempts (router logs each), then SchemaError rolls the node back
    with factory() as s:
        a = _inst(s, "Usage Ltd")
        _source(s, a)
        seed_agents(s, MACRO)
        s.commit()
        comp_id = _company(s, a).id

    def extract(session, instrument_id, run_id, **kw):
        router.complete("extract", "p", Out, run_id, session=session,
                        client=Client(content="not json", tokens=7000), config=_cfg(10**9))
    monkeypatch.setattr(cg, "extract_facts", extract)
    s = factory()
    res = cg.run_company_agent(s, comp_id, "usage-run")
    s.rollback()        # even if the whole job transaction rolls back
    s.close()
    assert res.status == "failed"
    with factory() as o:
        n = o.scalar(select(func.coalesce(func.sum(RunLog.prompt_tokens + RunLog.completion_tokens), 0))
                     .where(RunLog.run_id == "usage-run", RunLog.role == "extract"))
        assert n == 21000
        # attributed to the company job's agent (the per-run cap scope)
        assert set(o.scalars(select(RunLog.agent_id).where(RunLog.role == "extract"))) == {comp_id}
