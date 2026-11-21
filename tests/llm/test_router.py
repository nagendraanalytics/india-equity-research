from types import SimpleNamespace

import pytest
from pydantic import BaseModel
from sqlalchemy import select

from ier.db.models import RunLog
from ier.llm import router
from ier.llm.router import BudgetExceeded, SchemaError, complete


class Out(BaseModel):
    answer: str


class FakeClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class FakeClient:
    """Mimics openai.OpenAI: client.chat.completions.create(...). Each call burns 2s."""

    def __init__(self, clock, contents, prompt_tokens=10, completion_tokens=5):
        self.clock = clock
        self.contents = list(contents)
        self.usage = (prompt_tokens, completion_tokens)
        self.calls = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        self.clock.now += 2.0
        content = self.contents.pop(0)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
            usage=SimpleNamespace(prompt_tokens=self.usage[0], completion_tokens=self.usage[1]),
        )


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def cfg():
    return {
        "base_url": "http://gpu-box:8000/v1",
        "models": {"extract": "small-x", "synthesise": "small-s", "reason": "big-r"},
        "caps": {
            "per_run": {"max_tokens": 1000, "max_model_seconds": 100},
            "per_day": {"max_tokens": 5000, "max_model_seconds": 500},
        },
    }


def call(session, clock, client, cfg, role="extract", run_id="r1"):
    return complete(role, "hello", Out, run_id, session=session, client=client, clock=clock, config=cfg)


def test_role_maps_to_configured_model_and_base_url(session, clock, cfg, monkeypatch):
    client = FakeClient(clock, ['{"answer": "a"}', '{"answer": "b"}'])
    assert call(session, clock, client, cfg, "extract") == Out(answer="a")
    call(session, clock, client, cfg, "reason")
    assert [c["model"] for c in client.calls] == ["small-x", "big-r"]
    assert client.calls[0]["response_format"] == {"type": "json_object"}
    assert "answer" in client.calls[0]["messages"][-1]["content"]  # schema embedded in prompt

    seen = {}

    class Spy:
        def __init__(self, **kw):
            seen.update(kw)
            self.chat = FakeClient(clock, ['{"answer": "c"}']).chat

    monkeypatch.setattr(router.openai, "OpenAI", Spy)
    complete("synthesise", "hi", Out, "r2", session=session, clock=clock, config=cfg)
    assert seen["base_url"] == "http://gpu-box:8000/v1"


def test_default_config_loads():
    c = router.load_config()
    assert c["base_url"] == "http://localhost:11434/v1"
    assert c["models"] == {
        "extract": "qwen2.5:14b-instruct",
        "synthesise": "qwen2.5:14b-instruct",
        "reason": "qwen2.5:32b-instruct",
    }
    assert set(c["caps"]) == {"per_run", "per_day"}


def test_unknown_role_rejected(session, clock, cfg):
    client = FakeClient(clock, [])
    with pytest.raises(ValueError, match="role"):
        call(session, clock, client, cfg, "chat")
    assert client.calls == []


def test_invalid_json_retries_twice_then_raises_schema_error(session, clock, cfg):
    client = FakeClient(clock, ["nope", '{"wrong": 1}', "{bad"])
    with pytest.raises(SchemaError):
        call(session, clock, client, cfg)
    assert len(client.calls) == 3


def test_retry_then_success(session, clock, cfg):
    client = FakeClient(clock, ["nope", '{"answer": "ok"}'])
    assert call(session, clock, client, cfg).answer == "ok"
    assert len(client.calls) == 2


def test_cap_exceeded_raises_before_call(session, clock, cfg):
    cfg["caps"]["per_run"]["max_tokens"] = 30
    client = FakeClient(clock, ['{"answer": "a"}'] * 3)
    call(session, clock, client, cfg)  # 15 used
    call(session, clock, client, cfg)  # 30 used -> at cap
    with pytest.raises(BudgetExceeded, match="per_run.*tokens"):
        call(session, clock, client, cfg)
    assert len(client.calls) == 2


def test_model_seconds_cap(session, clock, cfg):
    cfg["caps"]["per_run"]["max_model_seconds"] = 2
    client = FakeClient(clock, ['{"answer": "a"}'] * 2)
    call(session, clock, client, cfg)
    with pytest.raises(BudgetExceeded, match="per_run.*model_seconds"):
        call(session, clock, client, cfg)
    assert len(client.calls) == 1


def test_daily_cap_counts_other_runs(session, clock, cfg):
    cfg["caps"]["per_day"]["max_tokens"] = 30
    client = FakeClient(clock, ['{"answer": "a"}'] * 3)
    call(session, clock, client, cfg, run_id="a")
    call(session, clock, client, cfg, run_id="b")
    with pytest.raises(BudgetExceeded, match="per_day.*tokens"):
        call(session, clock, client, cfg, run_id="c")
    assert len(client.calls) == 2


def test_usage_recorded_in_run_log(session, clock, cfg):
    client = FakeClient(clock, ['{"answer": "a"}'], prompt_tokens=11, completion_tokens=7)
    call(session, clock, client, cfg, "reason", run_id="run-9")
    row = session.scalars(select(RunLog).where(RunLog.run_id == "run-9")).one()
    assert (row.role, row.model_version) == ("reason", "big-r")
    assert (row.prompt_tokens, row.completion_tokens) == (11, 7)
    assert row.model_seconds == pytest.approx(2.0)
    assert row.prompts == "hello"
    assert row.outputs == {"answer": "a"}


def test_usage_counts_failed_attempts(session, clock, cfg):
    client = FakeClient(clock, ["bad", "bad", "bad"])
    with pytest.raises(SchemaError):
        call(session, clock, client, cfg, run_id="rf")
    rows = session.scalars(select(RunLog).where(RunLog.run_id == "rf")).all()
    assert len(rows) == 3
    assert sum(r.prompt_tokens + r.completion_tokens for r in rows) == 45
    assert sum(r.model_seconds for r in rows) == pytest.approx(6.0)


def test_client_exception_logs_usage_and_reraises(session, clock, cfg):
    def boom(**kw):
        clock.now += 5.0
        raise TimeoutError("slow")

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=boom)))
    with pytest.raises(TimeoutError):
        call(session, clock, client, cfg, run_id="rx")
    row = session.scalars(select(RunLog).where(RunLog.run_id == "rx")).one()
    assert row.model_seconds == pytest.approx(5.0)
    assert row.completion_tokens == 0 and row.prompt_tokens > 0


def test_client_built_with_timeout_no_retries_and_max_tokens(session, clock, cfg, monkeypatch):
    cfg["timeout_seconds"] = 33
    cfg["max_output_tokens"] = 77
    seen = {}
    inner = FakeClient(clock, ['{"answer": "c"}'])

    class Spy:
        def __init__(self, **kw):
            seen.update(kw)
            self.chat = inner.chat

    monkeypatch.setattr(router.openai, "OpenAI", Spy)
    complete("extract", "hi", Out, "rt", session=session, clock=clock, config=cfg)
    assert seen["timeout"] == 33 and seen["max_retries"] == 0
    assert inner.calls[0]["max_tokens"] == 77


def test_missing_usage_is_estimated_and_counts_toward_cap(session, clock, cfg):
    client = FakeClient(clock, ['{"answer": "a"}'] * 2)
    orig = client._create

    def no_usage(**kw):
        r = orig(**kw)
        r.usage = None
        return r

    client.chat.completions.create = no_usage
    call(session, clock, client, cfg, run_id="ru")
    row = session.scalars(select(RunLog).where(RunLog.run_id == "ru")).one()
    assert row.prompt_tokens > 0 and row.completion_tokens > 0
    cfg["caps"]["per_run"]["max_tokens"] = row.prompt_tokens + row.completion_tokens
    with pytest.raises(BudgetExceeded):
        call(session, clock, client, cfg, run_id="ru")


@pytest.mark.parametrize(
    "content",
    ['```json\n{"answer": "f"}\n```', 'Here is the JSON: {"answer": "f"} hope it helps'],
)
def test_fenced_or_prefixed_json_validates_first_attempt(session, clock, cfg, content):
    client = FakeClient(clock, [content])
    assert call(session, clock, client, cfg).answer == "f"
    assert len(client.calls) == 1


def test_retry_prompt_includes_validation_error(session, clock, cfg):
    client = FakeClient(clock, ['{"wrong": 1}', '{"answer": "ok"}'])
    assert call(session, clock, client, cfg, run_id="rp").answer == "ok"
    first = client.calls[0]["messages"][-1]["content"]
    second = client.calls[1]["messages"][-1]["content"]
    assert second != first and "validation error" in second
    rows = session.scalars(select(RunLog).where(RunLog.run_id == "rp")).all()
    assert {r.prompts for r in rows} == {"hello"}


# --- F2: cap scope and per-job attribution -------------------------------------------------

def test_budget_exceeded_names_its_scope(session, clock, cfg):
    session.add(RunLog(run_id="r1", prompt_tokens=1000, completion_tokens=0))
    session.flush()
    with pytest.raises(BudgetExceeded) as e:
        call(session, clock, FakeClient(clock, ['{"answer": "a"}']), cfg)
    assert e.value.scope == "per_run"
    session.add(RunLog(run_id="other", prompt_tokens=5000, completion_tokens=0))
    session.flush()
    with pytest.raises(BudgetExceeded) as e:
        call(session, clock, FakeClient(clock, ['{"answer": "a"}']), cfg, run_id="fresh")
    assert e.value.scope == "per_day"


def test_per_run_cap_counts_only_the_scoped_agents_usage(session, clock, cfg):
    from ier.db.models import Agent
    a, b = Agent(tier=5, role="company"), Agent(tier=5, role="company")
    session.add_all([a, b])
    session.flush()
    session.add(RunLog(run_id="r1", agent_id=a.id, prompt_tokens=1000, completion_tokens=0))
    session.flush()
    session.info[router.CAP_SCOPE_KEY] = b.id
    try:
        assert call(session, clock, FakeClient(clock, ['{"answer": "a"}']), cfg) == Out(answer="a")
        row = session.scalars(select(RunLog).where(RunLog.agent_id == b.id)).one()
        assert row.run_id == "r1"
        session.info[router.CAP_SCOPE_KEY] = a.id
        with pytest.raises(BudgetExceeded):
            call(session, clock, FakeClient(clock, ['{"answer": "a"}']), cfg)
    finally:
        session.info.pop(router.CAP_SCOPE_KEY, None)
