import asyncio
import json
import threading
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text

from ier.api.main import create_app, release
from ier.db.models import (Agent, AgentEvent, EvidenceLink, Fact, Instrument, Job, RunLog,
                           Scorecard, Signal, Source)
from ier.runtime.events import emit, seed_agents, tree
from tests.api.conftest import SSE, TODAY
from tests.runtime.test_events import MACRO, _inst

FORBIDDEN = {"buy", "sell", "order", "quantity", "price_target"}
LABEL = "research signal — not an order or investment advice"


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 20))


def commit(factory, fn):
    with factory() as s:
        out = fn(s)
        s.commit()
        return out


class _Id:
    def __init__(self, id):
        self.id = id


def raw_insert(session, agent_id):
    """Insert an event row inside the caller's OPEN transaction (emit() no longer does)."""
    return _Id(session.execute(text(
        "insert into agent_events(agent_id, kind, payload, cost) "
        "values (:a, 'step', '{}', 0) returning id"), {"a": agent_id}).scalar_one())


def burn_id(factory):
    """Consume one event id that never appears (as a rolled-back insert would)."""
    commit(factory, lambda s: s.execute(text("select nextval('agent_events_id_seq')")))


def keys(obj, acc=None):
    acc = set() if acc is None else acc
    if isinstance(obj, dict):
        for k, v in obj.items():
            acc.add(str(k).lower())
            keys(v, acc)
    elif isinstance(obj, list):
        for v in obj:
            keys(v, acc)
    return acc


def world(factory):
    """Small universe seeded with agents; returns dict of ids."""
    def go(s):
        a = _inst(s, "Alpha Ltd", "Software", "IT Services")
        b = _inst(s, "Beta Ltd", "Software", "IT Services")
        c = _inst(s, "Gamma Ltd", "Hardware", "Devices")
        seed_agents(s, MACRO)
        s.flush()
        agents = {r.scope_instrument_id: r.id for r in s.scalars(
            select(Agent).where(Agent.role == "company"))}
        master = s.scalar(select(Agent).where(Agent.role == "master"))
        worker = s.scalar(select(Agent).where(Agent.role == "fundamentals"))
        sector = s.scalar(select(Agent).where(Agent.role == "sector"))
        return dict(a=a.id, b=b.id, c=c.id, isin_a=a.isin, isin_b=b.isin, isin_c=c.isin,
                    ca=agents[a.id], cb=agents[b.id], cc=agents[c.id], master=master.id,
                    worker=worker.id, sector=sector.id)
    return commit(factory, go)


def add_card(factory, inst_id, long, short, run_date=TODAY, conf="high", rating=None,
             rules="v1", flags=None):
    def go(s):
        sc = Scorecard(instrument_id=inst_id, run_date=run_date, long_term_score=long,
                       short_term_score=short, confidence=conf, rules_version=rules,
                       sub_scores={"growth": 70.0}, benefits=["b"], risks=["r"],
                       red_flags=flags or [])
        s.add(sc)
        s.flush()
        if rating:
            s.add(Signal(instrument_id=inst_id, rating=rating, scorecard_id=sc.id,
                         rules_version=rules, run_date=run_date, reasons=["because"]))
        return sc.id
    return commit(factory, go)


# --- agents -----------------------------------------------------------------------------

def test_tree_endpoint_returns_nested_status(client, factory):
    w = world(factory)
    commit(factory, lambda s: (emit(s, w["ca"], "started", {"task": "company run"}, cost=2.5),
                               emit(s, w["cb"], "failed", {"error": "boom"})))
    with factory() as s:
        expected = tree(s)
        last = s.scalar(select(AgentEvent.id).order_by(AgentEvent.id.desc()).limit(1))
    r = client.get("/agents/tree")
    assert r.status_code == 200
    body = r.json()
    assert body["last_event_id"] == last

    def check(node, exp):
        assert node["id"] == exp.id and node["tier"] == exp.tier and node["name"] == exp.name
        assert node["status"] == exp.status
        assert node["current_task"] == exp.current_task
        assert node["cost_today"] == pytest.approx(exp.cost_today)
        assert [c["id"] for c in node["children"]] == [c.id for c in exp.children]
        for c, e in zip(node["children"], exp.children):
            check(c, e)
    check(body["tree"], expected)
    assert body["tree"]["status"] == "running"      # rolled up from the running company
    assert body["tree"]["children"], "nested children expected"
    # a failed leaf shows as failed somewhere in the nesting
    def find(n, i):
        return n if n["id"] == i else next((f for c in n["children"] if (f := find(c, i))), None)
    assert find(body["tree"], w["cb"])["status"] == "failed"
    assert find(body["tree"], w["ca"])["current_task"] == "company run"


def test_tree_endpoint_without_agents_is_404_and_empty_log_has_zero(client, factory):
    assert client.get("/agents/tree").status_code == 404
    commit(factory, lambda s: seed_agents(s, MACRO) or _inst(s, "X", "Software", "IT Services"))
    # no instruments at seed time: only master + macro exist, log empty
    r = client.get("/agents/tree")
    assert r.status_code == 200 and r.json()["last_event_id"] == 0


def test_agent_detail_includes_cost_and_last_output(client, factory):
    w = world(factory)

    def go(s):
        emit(s, w["ca"], "started", {"task": "company run", "run_id": "r1"}, cost=1.0)
        emit(s, w["ca"], "failed", {"error": "first failure"})
        emit(s, w["ca"], "output", {"kind": "note", "text": "old"})
        emit(s, w["ca"], "output", {"kind": "note", "text": "latest output"}, cost=0.5)
        emit(s, w["ca"], "failed", {"error": "second failure"})
        emit(s, w["ca"], "started", {"task": "company run"})
        s.add(RunLog(agent_id=w["ca"], run_id="r1", prompt_tokens=10, completion_tokens=5,
                     model_seconds=0.5))
        # a worker of the OTHER company must not be counted
        s.add(RunLog(agent_id=w["cb"], run_id="r2", prompt_tokens=999, completion_tokens=1,
                     model_seconds=9.0))
    commit(factory, go)
    wk = commit(factory, lambda s: s.scalar(select(Agent.id).where(
        Agent.parent_id == w["ca"], Agent.role == "fundamentals")))
    commit(factory, lambda s: s.add(RunLog(agent_id=wk, run_id="r1", prompt_tokens=100,
                                           completion_tokens=40, model_seconds=2.0)))
    r = client.get(f"/agents/{w['ca']}")
    assert r.status_code == 200
    d = r.json()
    assert d["id"] == w["ca"] and d["role"] == "company" and d["name"] == "Alpha Ltd"
    assert d["status"] == "running" and d["current_task"] == "company run"
    assert d["cost"]["prompt_tokens"] == 110          # own 10 + worker 100 (subtree)
    assert d["cost"]["completion_tokens"] == 45
    assert d["cost"]["total_tokens"] == 155
    assert d["cost"]["model_seconds"] == pytest.approx(2.5)
    assert d["cost"]["event_cost_total"] == pytest.approx(1.5)
    assert d["last_output"]["payload"]["text"] == "latest output"
    assert d["last_error"]["message"] == "second failure"
    # other company: nothing leaks
    other = client.get(f"/agents/{w['cb']}").json()
    assert other["cost"]["prompt_tokens"] == 999 and other["last_output"] is None
    assert other["last_error"] is None
    assert client.get("/agents/999999").status_code == 404


def test_agent_events_endpoint_newest_first_and_limited(client, factory):
    w = world(factory)
    ids = commit(factory, lambda s: [emit(s, w["ca"], "step", {"n": i}).id for i in range(5)])
    commit(factory, lambda s: emit(s, w["cb"], "step", {"n": 99}))
    r = client.get(f"/agents/{w['ca']}/events?limit=3")
    assert [e["id"] for e in r.json()["events"]] == ids[::-1][:3]
    assert all(e["agent_id"] == w["ca"] for e in r.json()["events"])
    r2 = client.get(f"/agents/{w['ca']}/events?limit=3&before_id={ids[2]}")
    assert [e["id"] for e in r2.json()["events"]] == [ids[1], ids[0]]
    assert client.get(f"/agents/{w['ca']}/events?limit=0").status_code == 422
    assert client.get("/agents/999999/events").status_code == 404


def test_rerun_enqueues_job_and_emits_queued(client, factory):
    w = world(factory)
    r = client.post(f"/agents/{w['ca']}/rerun")
    assert r.status_code == 200
    body = r.json()
    with factory() as s:
        job = s.get(Job, body["job_id"])
        assert job.agent_id == w["ca"] and job.status == "queued" and job.trigger == "manual"
        assert job.scheduled_at is not None
        ev = s.scalars(select(AgentEvent).where(AgentEvent.agent_id == w["ca"],
                                                AgentEvent.kind == "queued")).all()
        assert len(ev) == 1 and ev[0].payload["job_id"] == job.id
        assert body["event_id"] == ev[0].id
    assert body["already_queued"] is False
    # a second click while still queued does not stack jobs
    r2 = client.post(f"/agents/{w['ca']}/rerun")
    assert r2.status_code == 200 and r2.json()["job_id"] == body["job_id"]
    assert r2.json()["already_queued"] is True
    with factory() as s:
        assert s.scalar(text("select count(*) from jobs")) == 1
        assert s.scalar(text("select count(*) from agent_events where kind='queued'")) == 1
    # while the job is RUNNING a new rerun creates a second job (running job absorbs nothing)
    commit(factory, lambda s: s.execute(text("update jobs set status='running'")))
    r3 = client.post(f"/agents/{w['ca']}/rerun")
    assert r3.json()["job_id"] != body["job_id"] and r3.json()["already_queued"] is False


def test_rerun_unknown_404_and_non_rerunnable_409(client, factory):
    w = world(factory)
    assert client.post("/agents/999999/rerun").status_code == 404
    for aid in (w["master"], w["worker"]):
        r = client.post(f"/agents/{aid}/rerun")
        assert r.status_code == 409
        assert "cannot be rerun" in r.json()["detail"]
    for aid in (w["sector"],):          # sector agents have a handler: allowed
        assert client.post(f"/agents/{aid}/rerun").status_code == 200
    with factory() as s:
        assert s.scalar(text("select count(*) from jobs where agent_id in "
                             f"({w['master']},{w['worker']})")) == 0


# --- stream -----------------------------------------------------------------------------

def test_stream_emits_new_event_to_connected_client(app, factory):
    w = world(factory)

    async def go():
        sse = await SSE(app).start()
        assert sse.status == 200
        assert sse.headers["content-type"].startswith("text/event-stream")
        assert sse.headers.get("cache-control", "").startswith("no-cache")
        # connected before the event exists
        eid = commit(factory, lambda s: emit(s, w["ca"], "started", {"task": "t"}, cost=1).id)
        (ev,) = await sse.events(1)
        assert ev["id"] == eid and ev["event"] == "agent_event"
        assert ev["data"]["id"] == eid and ev["data"]["agent_id"] == w["ca"]
        assert ev["data"]["kind"] == "started" and ev["data"]["payload"] == {"task": "t"}
        # events emitted before connecting are not replayed without Last-Event-ID
        await sse.disconnect()
    run(go())


def test_stream_skips_history_without_last_event_id(app, factory):
    w = world(factory)
    commit(factory, lambda s: emit(s, w["ca"], "step", {"old": True}))

    async def go():
        sse = await SSE(app).start()
        await sse.expect_silence(0.3)
        await sse.disconnect()
    run(go())


def test_reconnect_with_last_event_id_gets_no_duplicates_or_gaps(app, factory):
    w = world(factory)

    async def go():
        seen = []
        sse = await SSE(app).start()
        a = commit(factory, lambda s: [emit(s, w["ca"], "step", {"n": i}).id for i in range(2)])
        seen += [e["id"] for e in await sse.events(2)]
        assert seen == a
        await sse.disconnect()
        # events written while the client is away, across several agents and transactions
        b = commit(factory, lambda s: [emit(s, w["ca"], "step", {"n": 10}).id,
                                       emit(s, w["cb"], "output", {"n": 11}).id])
        b += commit(factory, lambda s: [emit(s, w["cc"], "completed", {}).id])
        sse2 = await SSE(app, headers={"Last-Event-ID": str(seen[-1])}).start()
        got = [e["id"] for e in await sse2.events(3)]
        assert got == b == sorted(b)
        live = commit(factory, lambda s: emit(s, w["ca"], "step", {"n": 12}).id)
        got += [e["id"] for e in await sse2.events(1)]
        assert got == b + [live]
        await sse2.expect_silence(0.3)          # nothing repeated afterwards
        await sse2.disconnect()
        every = seen + got
        assert every == list(range(every[0], every[0] + len(every)))   # no gaps, no dups
        # reconnect from the middle: only strictly greater ids
        sse3 = await SSE(app, headers={"Last-Event-ID": str(b[0])}).start()
        assert [e["id"] for e in await sse3.events(3)] == b[1:] + [live]
        await sse3.disconnect()
    run(go())


def test_last_event_id_query_fallback_header_precedence_and_invalid(app, factory):
    w = world(factory)
    ids = commit(factory, lambda s: [emit(s, w["ca"], "step", {"n": i}).id for i in range(3)])

    async def go():
        q = await SSE(app, path=f"/stream?last_event_id={ids[0]}").start()
        assert [e["id"] for e in await q.events(2)] == ids[1:]
        await q.disconnect()
        h = await SSE(app, path=f"/stream?last_event_id={ids[0]}",
                      headers={"Last-Event-ID": str(ids[1])}).start()
        assert [e["id"] for e in await h.events(1)] == [ids[2]]
        await h.expect_silence(0.2)
        await h.disconnect()
    run(go())


def test_invalid_last_event_id_rejected(client):
    assert client.get("/stream", headers={"Last-Event-ID": "abc"}).status_code == 422
    assert client.get("/stream?last_event_id=-1").status_code == 422


def test_stream_stops_on_client_disconnect(app, factory):
    world(factory)

    async def go():
        sse = await SSE(app).start()
        await sse.disconnect(timeout=2)       # raises TimeoutError if the generator lingers
        assert sse.task.done()
    run(go())


def test_stream_sends_keepalive_comments(make_app, factory):
    app = make_app(keepalive_interval=0.05)
    world(factory)

    async def go():
        sse = await SSE(app).start()
        frames = await sse.frames(3, timeout=3)
        assert any(f.startswith(":") and "keep-alive" in f for f in frames[1:])
        await sse.disconnect()
    run(go())


def test_stream_does_not_skip_event_committed_late_out_of_order(make_app, factory):
    """Tx A takes id n but commits after tx B (id n+1) is already visible: the stream must
    hold n+1 back until n appears, then deliver both in order."""
    app = make_app(gap_grace=5.0)
    w = world(factory)
    a_session = factory()
    a = raw_insert(a_session, w["ca"])                         # id n, uncommitted
    b = commit(factory, lambda s: emit(s, w["cb"], "step", {"who": "B"}).id)
    assert b == a.id + 1

    async def go():
        try:
            sse = await SSE(app, headers={"Last-Event-ID": str(a.id - 1)}).start()
            await sse.expect_silence(0.4)       # B is visible but held back behind the gap
            a_session.commit()
            got = [e["id"] for e in await sse.events(2)]
            assert got == [a.id, b]
            await sse.disconnect()
        finally:
            a_session.close()                   # never leave a tx open (would block teardown)
    run(go())


def test_stream_releases_after_grace_when_gap_is_permanent(make_app, factory):
    app = make_app(gap_grace=0.2)
    w = world(factory)
    def burn(s):
        s.execute(text("select nextval('agent_events_id_seq')"))   # id that never appears
    start = commit(factory, lambda s: emit(s, w["ca"], "step", {}).id)
    commit(factory, burn)
    nxt = commit(factory, lambda s: emit(s, w["ca"], "step", {"n": 2}).id)
    assert nxt == start + 2

    async def go():
        sse = await SSE(app, headers={"Last-Event-ID": str(start)}).start()
        (ev,) = await sse.events(1, timeout=3)
        assert ev["id"] == nxt
        await sse.disconnect()
    run(go())


# --- analyst ----------------------------------------------------------------------------

def test_analyst_endpoint_lists_cited_evidence(client, factory):
    w = world(factory)

    def go(s):
        src = Source(url="https://www.bseindia.com/x.pdf", type="annual_report", tier=1,
                     publish_date=date(2026, 3, 31),
                     fetch_date=datetime(2026, 11, 1, 10, 0, tzinfo=timezone.utc))
        src2 = Source(url="https://news.example/y", type="news", tier=3)
        s.add_all([src, src2])
        s.flush()
        fact = Fact(instrument_id=w["a"], source_id=src.id, metric="revenue", value=123.5,
                    unit="INR cr", period="FY26", period_end=date(2026, 3, 31))
        s.add(fact)
        sc = Scorecard(instrument_id=w["a"], run_date=TODAY, long_term_score=71.0,
                       short_term_score=44.0, confidence="medium", rules_version="it-v3",
                       sub_scores={"growth": 80.0, "quality": 62.5}, benefits=["strong growth"],
                       risks=["concentration"], red_flags=["promoter pledge"])
        old = Scorecard(instrument_id=w["a"], run_date=TODAY - timedelta(days=30),
                        long_term_score=10.0, short_term_score=10.0, confidence="low",
                        rules_version="it-v1", sub_scores={}, red_flags=[])
        s.add_all([sc, old])
        s.flush()
        s.add(EvidenceLink(scorecard_id=sc.id, claim_type="score", claim_ref="growth",
                           fact_id=fact.id, source_id=src.id))
        s.add(RunLog(agent_id=w["ca"], run_id="run-1", role="company", model_version="m1",
                     prompt_tokens=10, completion_tokens=5, model_seconds=1.5,
                     inputs={"force": True, "fingerprint": "abc"},
                     outputs={"status": "completed", "scorecard_id": sc.id}))
        s.add(RunLog(agent_id=w["cb"], run_id="other", role="company"))
        return src.id
    src_id = commit(factory, go)
    r = client.get(f"/analysts/{w['ca']}")
    assert r.status_code == 200
    d = r.json()
    assert d["agent_id"] == w["ca"] and d["isin"] == w["isin_a"] and d["name"] == "Alpha Ltd"
    f = d["findings"]
    assert f["sub_scores"] == {"growth": 80.0, "quality": 62.5}
    assert f["red_flags"] == ["promoter pledge"] and f["confidence"] == "medium"
    assert f["long_term_score"] == 71.0 and f["short_term_score"] == 44.0
    assert f["benefits"] == ["strong growth"] and f["risks"] == ["concentration"]
    assert d["rule_inputs"]["rules_version"] == "it-v3"
    assert d["rule_inputs"]["scorecard_inputs"]["sub_scores"] == {"growth": 80.0, "quality": 62.5}
    assert d["rule_inputs"]["scorecard_run_date"] == TODAY.isoformat()
    (ev,) = d["evidence"]
    assert ev["claim_type"] == "score" and ev["claim_ref"] == "growth"
    assert ev["fact"]["metric"] == "revenue" and ev["fact"]["value"] == 123.5
    assert ev["fact"]["period"] == "FY26" and ev["fact"]["as_of"] == "2026-03-31"
    assert ev["source"]["id"] == src_id and ev["source"]["url"] == "https://www.bseindia.com/x.pdf"
    assert ev["source"]["tier"] == 1 and ev["source"]["fetched_at"].startswith("2026-11-01T10:00")
    assert ev["source"]["publish_date"] == "2026-03-31"
    assert [h["run_id"] for h in d["run_history"]] == ["run-1"]
    h = d["run_history"][0]
    assert h["status"] == "completed" and h["total_tokens"] == 15 and h["model_version"] == "m1"
    assert d["freshness"]["age_days"] == 0 and d["freshness"]["stale"] is False
    assert d["rules_version"] == "it-v3" and d["confidence"] == "medium"


def test_analyst_endpoint_errors_and_no_scorecard(client, factory):
    w = world(factory)
    assert client.get("/analysts/999999").status_code == 404
    assert client.get(f"/analysts/{w['master']}").status_code == 404   # not a company/worker
    d = client.get(f"/analysts/{w['ca']}").json()
    assert d["findings"] is None and d["evidence"] == [] and d["run_history"] == []
    assert d["freshness"]["stale"] is True and d["freshness"]["age_days"] is None


# --- stocks / sectors / signals ---------------------------------------------------------

def test_signals_include_confidence_freshness_and_label(client, factory):
    w = world(factory)
    add_card(factory, w["a"], 80, 70, conf="high", rating="accumulate", rules="it-v3")
    add_card(factory, w["b"], 30, 20, conf="low", rating="insufficient evidence",
             run_date=TODAY - timedelta(days=3), rules="it-v2")
    add_card(factory, w["c"], 40, 40, conf="medium", rating="reduce")
    r = client.get("/signals")
    assert r.status_code == 200
    body = r.json()
    assert body["label"] == LABEL
    items = {i["isin"]: i for i in body["items"]}
    assert set(items) == {w["isin_a"], w["isin_b"], w["isin_c"]}
    a, b = items[w["isin_a"]], items[w["isin_b"]]
    assert a["rating"] == "accumulate" and a["confidence"] == "high" and a["rules_version"] == "it-v3"
    assert a["label"] == LABEL and a["reasons"] == ["because"]
    assert a["age_days"] == 0 and a["stale"] is False and a["as_of"] == TODAY.isoformat()
    assert b["age_days"] == 3 and b["rules_version"] == "it-v2" and b["confidence"] == "low"
    assert body["stale_threshold_days"] == 7 and body["stale_threshold_status"] == "PLACEHOLDER"
    assert keys(body).isdisjoint(FORBIDDEN)
    # filters
    assert [i["isin"] for i in client.get("/signals?sector=Hardware").json()["items"]] == [w["isin_c"]]
    sw = client.get("/signals?rating=insufficient evidence").json()["items"]
    assert [i["isin"] for i in sw] == [w["isin_b"]]
    both = client.get("/signals?sector=Software&rating=accumulate").json()["items"]
    assert [i["isin"] for i in both] == [w["isin_a"]]
    assert client.get("/signals?rating=buy").status_code == 422
    assert client.get("/signals?rating=Accumulate").status_code == 422
    assert client.get("/signals?sector=Nope").json()["items"] == []


def test_signals_use_latest_signal_per_stock(client, factory):
    w = world(factory)
    add_card(factory, w["a"], 80, 70, run_date=TODAY - timedelta(days=2), rating="avoid")
    add_card(factory, w["a"], 80, 70, run_date=TODAY, rating="hold")
    items = client.get("/signals").json()["items"]
    assert [(i["isin"], i["rating"]) for i in items] == [(w["isin_a"], "hold")]


def test_stale_data_age_exposed(make_app, factory):
    w = world(factory)
    add_card(factory, w["a"], 50, 50, run_date=TODAY - timedelta(days=30), rating="hold")
    add_card(factory, w["b"], 50, 50, run_date=TODAY - timedelta(days=7), rating="hold")
    add_card(factory, w["c"], 50, 50, run_date=TODAY - timedelta(days=8), rating="hold")
    from starlette.testclient import TestClient
    with TestClient(make_app()) as c:
        stocks = {i["isin"]: i for i in c.get("/stocks").json()["items"]}
        assert stocks[w["isin_a"]]["age_days"] == 30 and stocks[w["isin_a"]]["stale"] is True
        assert stocks[w["isin_a"]]["as_of"] == (TODAY - timedelta(days=30)).isoformat()
        assert stocks[w["isin_b"]]["age_days"] == 7 and stocks[w["isin_b"]]["stale"] is False
        assert stocks[w["isin_c"]]["age_days"] == 8 and stocks[w["isin_c"]]["stale"] is True
        sig = {i["isin"]: i for i in c.get("/signals").json()["items"]}
        assert sig[w["isin_a"]]["age_days"] == 30 and sig[w["isin_a"]]["stale"] is True
        det = c.get(f"/stocks/{w['isin_a']}").json()
        assert det["age_days"] == 30 and det["stale"] is True
        sec = {i["sector"]: i for i in c.get("/sectors").json()["items"]}
        assert sec["Software"]["age_days"] == 30 and sec["Software"]["stale"] is True  # oldest
    # threshold comes from config, not code
    with TestClient(make_app(config={"stale_after_days": 40})) as c:
        stocks = {i["isin"]: i for i in c.get("/stocks").json()["items"]}
        assert stocks[w["isin_a"]]["stale"] is False
        assert c.get("/stocks").json()["stale_threshold_days"] == 40
    # a stock with no scorecard at all is not reported fresh
    commit(factory, lambda s: _inst(s, "Nodata Ltd", "Software", "IT Services"))
    with TestClient(make_app()) as c:
        nd = [i for i in c.get("/stocks").json()["items"] if i["name"] == "Nodata Ltd"][0]
        assert nd["age_days"] is None and nd["stale"] is True and nd["confidence"] is None


def test_default_stale_threshold_is_placeholder_config():
    from ier.api.main import load_api_config
    assert load_api_config()["stale_after_days"] == 7


def test_stocks_sortable_by_long_term_score(client, factory):
    w = world(factory)
    d = commit(factory, lambda s: _inst(s, "Delta Ltd", "Hardware", "Devices").id)
    e = commit(factory, lambda s: _inst(s, "Echo Ltd", "Software", "Products").id)
    add_card(factory, w["a"], 80, 10)
    add_card(factory, w["b"], 20, 90)
    add_card(factory, w["c"], 50, 50)
    add_card(factory, d, None, 70)       # null long-term score
    # e has no scorecard at all

    def order(q):
        r = client.get(f"/stocks?{q}")
        assert r.status_code == 200, r.text
        return [i["name"] for i in r.json()["items"]]
    assert order("sort=long_term_score") == [
        "Beta Ltd", "Gamma Ltd", "Alpha Ltd", "Delta Ltd", "Echo Ltd"]
    assert order("sort=-long_term_score") == [
        "Alpha Ltd", "Gamma Ltd", "Beta Ltd", "Delta Ltd", "Echo Ltd"]   # nulls last both ways
    assert order("sort=-short_term_score") == [
        "Beta Ltd", "Delta Ltd", "Gamma Ltd", "Alpha Ltd", "Echo Ltd"]
    assert order("sort=short_term_score")[:4] == ["Alpha Ltd", "Gamma Ltd", "Delta Ltd", "Beta Ltd"]
    assert order("sort=name") == ["Alpha Ltd", "Beta Ltd", "Delta Ltd", "Echo Ltd", "Gamma Ltd"]
    assert order("sort=-name")[0] == "Gamma Ltd"
    assert client.get("/stocks?sort=price_target").status_code == 422
    assert client.get("/stocks?sort=-bogus").status_code == 422
    assert client.get("/stocks?sort=").status_code == 422
    # filters
    assert order("sector=Hardware&sort=name") == ["Delta Ltd", "Gamma Ltd"]
    assert order("industry=Products") == ["Echo Ltd"]
    assert order("sector=Software&industry=IT Services&sort=-long_term_score") == [
        "Alpha Ltd", "Beta Ltd"]
    assert order("sector=Nope") == []
    item = client.get("/stocks?sort=name").json()["items"][0]
    assert item["rules_version"] == "v1" and item["confidence"] == "high"
    assert keys(item).isdisjoint(FORBIDDEN)


def test_stock_detail_and_unknown_isin_404(client, factory):
    w = world(factory)
    add_card(factory, w["a"], 80, 60, conf="medium", rating="hold", rules="it-v3",
             flags=["x"])
    d = client.get(f"/stocks/{w['isin_a']}").json()
    assert d["isin"] == w["isin_a"] and d["sector"] == "Software" and d["industry"] == "IT Services"
    assert d["confidence"] == "medium" and d["rules_version"] == "it-v3"
    assert d["sub_scores"] == {"growth": 70.0} and d["red_flags"] == ["x"]
    assert d["signal"]["rating"] == "hold" and d["signal"]["label"] == LABEL
    assert d["agent_id"] == w["ca"]
    assert client.get("/stocks/INE000000000").status_code == 404
    nod = client.get(f"/stocks/{w['isin_b']}").json()
    assert nod["signal"] is None and nod["long_term_score"] is None


def test_sectors_endpoint(client, factory):
    w = world(factory)
    add_card(factory, w["a"], 80, 60, conf="high", rating="accumulate", rules="it-v3")
    add_card(factory, w["b"], 40, 20, conf="low", rating="insufficient evidence", rules="it-v3",
             run_date=TODAY - timedelta(days=2))
    add_card(factory, w["c"], 10, 10, conf="medium", rating="avoid", rules="it-v3")
    body = client.get("/sectors").json()
    sec = {i["sector"]: i for i in body["items"]}
    sw, hw = sec["Software"], sec["Hardware"]
    assert sw["stock_count"] == 2 and sw["scored_count"] == 2
    assert sw["avg_long_term_score"] == pytest.approx(60.0)
    assert sw["avg_short_term_score"] == pytest.approx(40.0)
    assert sw["confidence"] == "low"                      # weakest across members
    assert sw["confidence_counts"] == {"high": 1, "low": 1}
    assert sw["signal_counts"] == {"accumulate": 1, "insufficient evidence": 1}
    assert sw["age_days"] == 2 and sw["rules_version"] == "it-v3" and sw["industries"] == ["IT Services"]
    assert hw["stock_count"] == 1 and hw["confidence"] == "medium"
    assert body["label"] == LABEL
    assert keys(body).isdisjoint(FORBIDDEN)


# --- cross-cutting ----------------------------------------------------------------------

def test_cors_allows_only_dashboard_origin(client):
    ok = client.get("/sectors", headers={"Origin": "http://localhost:3000"})
    assert ok.headers["access-control-allow-origin"] == "http://localhost:3000"
    bad = client.get("/sectors", headers={"Origin": "http://evil.example"})
    assert "access-control-allow-origin" not in bad.headers
    pre = client.options("/agents/1/rerun", headers={
        "Origin": "http://localhost:3000", "Access-Control-Request-Method": "POST"})
    assert pre.headers["access-control-allow-origin"] == "http://localhost:3000"


def test_api_is_read_only_except_rerun_and_has_no_order_fields(app):
    writes = {(r.path, m) for r in app.routes if hasattr(r, "methods")
              for m in r.methods if m not in ("GET", "HEAD", "OPTIONS")}
    assert writes == {("/agents/{agent_id}/rerun", "POST"), ("/sources/{source_id}/check", "POST")}
    schema = app.openapi()
    names = set()

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k == "properties":
                    names.update(str(p).lower() for p in v)
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(schema)
    params = {p["name"].lower() for path in schema["paths"].values() for op in path.values()
              for p in op.get("parameters", [])}
    assert names.isdisjoint(FORBIDDEN) and params.isdisjoint(FORBIDDEN)
    # every endpoint has a typed response model
    for path, ops in schema["paths"].items():
        if path == "/stream":
            continue
        for op in ops.values():
            assert "$ref" in op["responses"]["200"]["content"]["application/json"]["schema"], path


def test_module_level_app_importable_without_database(monkeypatch):
    monkeypatch.setenv("IER_DATABASE_URL", "postgresql+psycopg://nobody@127.0.0.1:1/none")
    import importlib
    import ier.api.main as m
    importlib.reload(m)
    assert m.app.title


# --- fix round 1 ------------------------------------------------------------------------

def test_release_continues_contiguity_check_after_expired_gap():
    out, gap = release([12, 14], 10, (11, 100.0), 110.0, 2.0)     # gap at 11 expired
    assert out == [12]                       # 14 sits behind a NEW, unexpired gap at 13
    assert gap is not None and gap[0] == 13
    out2, gap2 = release([14], 12, gap, gap[1] + 5, 2.0)         # 13 never shows: now expired
    assert out2 == [14] and gap2 is None
    assert release([11, 12], 10, None, 0.0, 2.0) == ([11, 12], None)


def test_long_job_transaction_events_are_delivered_once_in_order(make_app, factory):
    """A worker holds its job transaction open far longer than gap_grace; events it emitted are
    visible at emit time and must be delivered exactly once, in id order, with no gaps."""
    app = make_app(gap_grace=0.15)
    w = world(factory)
    job = factory()
    job.execute(text("select 1"))            # job transaction is open from here on

    async def go():
        try:
            sse = await SSE(app).start()
            e1 = emit(job, w["ca"], "started", {"job_id": 1}).id        # job event
            other = commit(factory, lambda s: emit(s, w["cb"], "step", {"x": 1}).id)
            await asyncio.sleep(0.5)                                     # >> gap_grace
            e2 = emit(job, w["ca"], "step", {"node": "n"}).id           # still same open tx
            e3 = emit(job, w["ca"], "completed", {"job_id": 1}).id
            got = [e["id"] for e in await sse.events(4)]
            assert got == [e1, other, e2, e3] == sorted(got)
            assert len(set(got)) == 4 and got == list(range(got[0], got[0] + 4))
            await sse.expect_silence(0.3)                                # exactly once
            await sse.disconnect()
        finally:
            job.rollback()
            job.close()
        with factory() as s:                 # events of the rolled-back job stay in the log
            assert s.scalar(text("select count(*) from agent_events")) == 4
    run(go())


def test_tree_resync_does_not_skip_inflight_lower_id(make_app, factory):
    from starlette.testclient import TestClient
    w = world(factory)
    app = make_app(gap_grace=5.0)
    base = commit(factory, lambda s: emit(s, w["ca"], "step", {}).id)
    open_tx = factory()
    low = raw_insert(open_tx, w["ca"]).id                  # in flight (uncommitted)
    high = commit(factory, lambda s: emit(s, w["cb"], "step", {}).id)
    assert low == base + 1 and high == low + 1
    try:
        with TestClient(app) as c:
            last = c.get("/agents/tree").json()["last_event_id"]
        assert last == base                                # watermark stops below the gap
        open_tx.commit()

        async def go():
            sse = await SSE(app, headers={"Last-Event-ID": str(last)}).start()
            assert [e["id"] for e in await sse.events(2)] == [low, high]
            await sse.expect_silence(0.3)
            await sse.disconnect()
        run(go())
    finally:
        open_tx.close()


def test_stream_default_start_does_not_skip_inflight_lower_id(make_app, factory):
    w = world(factory)
    app = make_app(gap_grace=5.0)
    open_tx = factory()
    low = raw_insert(open_tx, w["ca"]).id
    high = commit(factory, lambda s: emit(s, w["cb"], "step", {}).id)

    async def go():
        try:
            sse = await SSE(app).start()                    # no Last-Event-ID
            open_tx.commit()
            assert [e["id"] for e in await sse.events(2)] == [low, high]
            await sse.disconnect()
        finally:
            open_tx.close()
    run(go())


def test_tree_last_event_id_is_max_settled_id_with_id_gaps(make_app, factory):
    from starlette.testclient import TestClient
    import time
    w = world(factory)
    ids = commit(factory, lambda s: [emit(s, w["ca"], "step", {}).id for _ in range(2)])
    burn_id(factory)
    ids += commit(factory, lambda s: [emit(s, w["cb"], "step", {}).id for _ in range(2)])
    burn_id(factory)
    time.sleep(0.6)                                        # gaps are older than gap_grace
    with TestClient(make_app(gap_grace=0.5)) as c:
        body = c.get("/agents/tree").json()
    assert ids[-1] - ids[0] + 1 == 5 and len(ids) == 4     # real gaps exist: count(*) != max
    assert body["last_event_id"] == ids[-1]
    # a fresh gap (younger than grace) holds the watermark below it
    burn_id(factory)
    top = commit(factory, lambda s: emit(s, w["cc"], "step", {}).id)
    with TestClient(make_app(gap_grace=0.5)) as c:
        assert c.get("/agents/tree").json()["last_event_id"] == ids[-1]
    assert top > ids[-1] + 1


@pytest.mark.parametrize("bad", ["9223372036854775808", "-1", "abc", "1.5", "99999999999999999999"])
def test_last_event_id_out_of_range_or_non_numeric_is_422(client, bad):
    assert client.get("/stream", headers={"Last-Event-ID": bad}).status_code == 422
    assert client.get(f"/stream?last_event_id={bad}").status_code == 422


def test_worker_analyst_uses_company_run_history_and_inputs(client, factory):
    w = world(factory)
    wk = commit(factory, lambda s: s.scalar(select(Agent.id).where(
        Agent.parent_id == w["ca"], Agent.role == "fundamentals")))
    wk2 = commit(factory, lambda s: s.scalar(select(Agent.id).where(
        Agent.parent_id == w["ca"], Agent.role == "synthesis")))

    def go(s):
        s.add(RunLog(agent_id=w["ca"], run_id="company-run", role="company",
                     inputs={"fingerprint": "fp"}, outputs={"status": "completed"}))
        s.add(RunLog(agent_id=wk2, run_id="sibling-worker", role="synthesis"))
        s.add(RunLog(agent_id=wk, run_id="own-worker", role="extract",
                     inputs={"worker": True}))
    commit(factory, go)
    d = client.get(f"/analysts/{wk}").json()
    assert d["name"] == "Alpha Ltd" and d["agent_id"] == wk
    assert {h["run_id"] for h in d["run_history"]} == {"company-run", "own-worker", "sibling-worker"}
    # the worker's own RunLog is the newest row (inserted last) and it is part of the company
    # history, which is what the latest-run inputs are read from
    assert d["rule_inputs"]["latest_run_inputs"] == {"worker": True}


def test_api_config_loads_from_package_data_and_is_packaged():
    from importlib import resources
    from ier.api.main import load_api_config
    res = resources.files("ier.api").joinpath("config/api.yaml")
    assert res.is_file() and "PLACEHOLDER" in res.read_text()
    assert load_api_config()["stale_after_days"] == 7
    import tomllib
    from pathlib import Path
    pp = tomllib.loads((Path(__file__).resolve().parents[2] / "pyproject.toml").read_text())
    assert "config/*.yaml" in pp["tool"]["setuptools"]["package-data"]["ier.api"]


# --- fix round 2 ------------------------------------------------------------------------

def test_empty_last_event_id_header_is_absent_so_query_param_works(app, factory):
    w = world(factory)
    ids = commit(factory, lambda s: [emit(s, w["ca"], "step", {"n": i}).id for i in range(3)])

    async def go():
        sse = await SSE(app, path=f"/stream?last_event_id={ids[0]}",
                        headers={"Last-Event-ID": ""}).start()
        assert [e["id"] for e in await sse.events(2)] == ids[1:]
        await sse.disconnect()
    run(go())


def test_watermark_not_fooled_by_old_created_at_of_a_long_transaction(factory):
    """created_at must be the insert time, not the transaction start: a row written late by a
    long transaction must not make the watermark jump over a lower id still in flight."""
    import time
    from ier.api.main import settled_watermark
    w = world(factory)
    longtx = factory()
    longtx.execute(text("select 1"))
    time.sleep(0.6)
    t2 = factory()
    low = raw_insert(t2, w["ca"]).id
    high = raw_insert(longtx, w["cb"]).id
    longtx.commit()
    try:
        with factory() as s:
            s.connection(execution_options={"isolation_level": "REPEATABLE READ"})
            assert settled_watermark(s, 0.5) < low
    finally:
        t2.rollback()
        t2.close()
        longtx.close()


# --- Task 16: console header summary ----------------------------------------------------

def test_agents_summary_counts_queue_compute_and_failures(client, factory):
    w = world(factory)
    # empty log: zeros
    r0 = client.get("/agents/summary")
    assert r0.status_code == 200
    assert r0.json() == {"queue_depth": 0, "compute_tokens_today": 0,
                         "compute_model_seconds_today": 0.0, "failures_today": 0,
                         "last_event_id": 0}

    def go(s):
        s.add_all([Job(agent_id=w["ca"], trigger="manual", status="queued"),
                   Job(agent_id=w["cb"], trigger="manual", status="queued"),
                   Job(agent_id=w["cc"], trigger="manual", status="running"),
                   Job(agent_id=w["cc"], trigger="manual", status="done")])
        now = datetime.now(timezone.utc)
        old = now - timedelta(days=3)
        s.add_all([RunLog(agent_id=w["ca"], prompt_tokens=100, completion_tokens=50,
                          model_seconds=1.5, created_at=now),
                   RunLog(agent_id=w["cb"], prompt_tokens=10, completion_tokens=5,
                          model_seconds=0.5, created_at=now),
                   RunLog(agent_id=w["cb"], prompt_tokens=9999, completion_tokens=9999,
                          model_seconds=99.0, created_at=old)])
        emit(s, w["ca"], "failed", {"error": "x"})
        emit(s, w["cb"], "failed", {"error": "y"})
        emit(s, w["cc"], "completed", {})
        s.flush()
        s.execute(text("insert into agent_events(agent_id, kind, payload, cost, created_at) "
                       "values (:a, 'failed', '{}', 0, now() - interval '3 days')"), {"a": w["ca"]})
    commit(factory, go)
    body = client.get("/agents/summary").json()
    assert body["queue_depth"] == 2                      # queued jobs only
    assert body["compute_tokens_today"] == 165           # today's run_log only
    assert body["compute_model_seconds_today"] == pytest.approx(2.0)
    assert body["failures_today"] == 2                   # failed events since UTC midnight
    with factory() as s:
        top = s.scalar(select(AgentEvent.id).order_by(AgentEvent.id.desc()).limit(1))
    assert isinstance(body["last_event_id"], int) and 0 <= body["last_event_id"] <= top
    assert set(body) == {"queue_depth", "compute_tokens_today", "compute_model_seconds_today",
                         "failures_today", "last_event_id"}


def test_rerun_job_forces_a_run(client, factory):
    """F4: POST /agents/{id}/rerun must never be skipped as 'unchanged'."""
    w = world(factory)
    body = client.post(f"/agents/{w['ca']}/rerun").json()
    with factory() as s:
        assert s.get(Job, body["job_id"]).payload == {"force": True}


def test_tree_reports_blocked_for_jobs_waiting_on_their_parent(client, factory):
    """F5: a job waiting on an unfinished parent job shows 'blocked', not 'queued'."""
    from ier.runtime.queue import enqueue
    w = world(factory)

    def go(s):
        parent = enqueue(s, w["sector"], "run")
        enqueue(s, w["ca"], "run", depends_on=parent.id)
    commit(factory, go)
    body = client.get("/agents/tree").json()

    def find(n, i):
        if n["id"] == i:
            return n
        return next((r for c in n["children"] if (r := find(c, i))), None)
    assert find(body["tree"], w["ca"])["status"] == "blocked"
    assert find(body["tree"], w["sector"])["status"] == "queued"
    schema = client.get("/openapi.json").json()["components"]["schemas"]["AgentNodeOut"]
    assert "blocked" in schema["properties"]["status"]["description"]


def test_stock_history_returns_points_oldest_first_with_rating(client, factory):
    w = world(factory)
    isin = w["isin_a"]
    add_card(factory, w["a"], 40.0, 30.0, run_date=TODAY - timedelta(days=2), rating="hold")
    add_card(factory, w["a"], 55.0, 45.0, run_date=TODAY)
    body = client.get(f"/stocks/{isin}/history").json()
    assert body["isin"] == isin and body["count"] == 2
    assert [p["long_term_score"] for p in body["items"]] == [40.0, 55.0]
    assert [p["rating"] for p in body["items"]] == ["hold", None]
    assert client.get("/stocks/INE0000NOPE0/history").status_code == 404
    assert client.get(f"/stocks/{isin}/history?limit=0").status_code == 422


def test_agents_metrics_dense_series_jobs_and_recent_failures(client, factory):
    w = world(factory)

    def go(s):
        s.add_all([Job(agent_id=w["ca"], trigger="manual", status="queued"),
                   Job(agent_id=w["cb"], trigger="manual", status="done")])
        s.add(RunLog(agent_id=w["ca"], prompt_tokens=100, completion_tokens=50, model_seconds=1.5,
                     created_at=datetime.now(timezone.utc)))
        emit(s, w["ca"], "started", {})
        emit(s, w["ca"], "failed", {"error": "boom"})
    commit(factory, go)
    r = client.get("/agents/metrics")
    assert r.status_code == 200
    d = r.json()
    assert len(d["events_by_minute"]) == 60 and len(d["tokens_by_hour"]) == 24
    assert sum(p["started"] for p in d["events_by_minute"]) == 1
    assert sum(p["failed"] for p in d["events_by_minute"]) == 1
    assert sum(p["tokens"] for p in d["tokens_by_hour"]) == 150
    assert d["jobs_by_status"] == {"queued": 1, "running": 0, "done": 1, "failed": 0}
    assert d["recent_failures"][0]["error"] == "boom" and d["recent_failures"][0]["agent_id"] == w["ca"]


def test_agents_activity_lists_lifecycle_events_across_agents_newest_first(client, factory):
    w = world(factory)

    def go(s):
        emit(s, w["ca"], "started", {"task": "x"})
        emit(s, w["cb"], "started", {})
        emit(s, w["ca"], "output", {"big": 1})
        emit(s, w["ca"], "completed", {})
    commit(factory, go)
    r = client.get("/agents/activity", params={"limit": 10})
    assert r.status_code == 200
    ev = r.json()["events"]
    assert [e["kind"] for e in ev] == ["completed", "started", "started"]   # 'output' excluded
    assert {e["agent_id"] for e in ev} == {w["ca"], w["cb"]}
    assert client.get("/agents/activity", params={"limit": 0}).status_code == 422
