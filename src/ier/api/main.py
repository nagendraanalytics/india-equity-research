"""Read-only HTTP API for the dashboard (Task 15).

SECURITY / DEPLOYMENT NOTE: this is a local, single-user tool with NO authentication. Bind it
to loopback only (127.0.0.1, which `python -m ier.api` does by default) and never expose it to a
network or the public internet: anyone who can reach it can read every research output and queue
agent reruns. CORS admits only the local dashboard origin http://localhost:3000.

The API cannot place orders and has no such concept. The only write is POST /agents/{id}/rerun,
which enqueues a research job through the existing queue. Signal ratings are research labels
(accumulate / hold / reduce / avoid / insufficient evidence), never instructions.

Live stream: GET /stream is server-sent events. Each frame is `id: <agent_events.id>`,
`event: agent_event`, `data: <json>`. A client that reconnects sends `Last-Event-ID` (or
`?last_event_id=`) and receives exactly the events with a greater id, in id order. To resync
fully, GET /agents/tree returns the status tree plus `last_event_id`; open the stream with that
id. (The tree is read after that id, so it is at least as new; replayed events are idempotent.)
Sequence ids are assigned before commit, so a later id can become visible before an earlier one;
the stream holds back events behind such a gap for `gap_grace` seconds before moving on, so a
slow transaction's event is not skipped.

Delivery guarantee: `queued` events and a job's terminal events are written in the job's own
transaction (so they cannot outlive a rollback) and are streamed live only if that transaction
commits within gap_grace of the insert. Clients (the dashboard) MUST resync from GET
/agents/tree on reconnect and periodically; the stream is a latency optimisation, not the
source of truth.
"""
from __future__ import annotations

import asyncio
import json
import time
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from importlib import resources
from typing import Callable, Literal

import yaml
from anyio import to_thread
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.orm import Session

from ier.api import schemas as S
from ier.db.models import (Agent, AgentEvent, Chunk, EvidenceLink, Fact, Instrument, Job, RunLog,
                           Scorecard, Signal, Source)
from ier.runtime.events import AgentNode, tree
from ier.runtime.queue import QUEUED, tier_names, tier_number
from ier.runtime.triggers import MANUAL, on_manual

API_CONFIG = "config/api.yaml"      # package data of ier.api (see pyproject package-data)
DASHBOARD_ORIGIN = "http://localhost:3000"
# Roles whose jobs have a registered handler (ier.graph.company.register_default_handlers).
RERUNNABLE_ROLES = ("company", "sector", "industry", "source", "segment")
RATING = Literal["accumulate", "hold", "reduce", "avoid", "insufficient evidence"]
SORT_KEYS = ("name", "sector", "industry", "long_term_score", "short_term_score", "as_of")
CONF_RANK = {"low": 0, "medium": 1, "high": 2}
STREAM_BATCH = 500
MAX_EVENT_ID = 2**63 - 1      # bigint


def load_api_config() -> dict:
    path = resources.files("ier.api").joinpath(API_CONFIG)
    cfg = yaml.safe_load(path.read_text()) or {}
    days = cfg.get("stale_after_days")
    if isinstance(days, bool) or not isinstance(days, int) or days < 0:
        raise ValueError(f"{path}: stale_after_days must be a non-negative integer")
    return cfg


# --- helpers ----------------------------------------------------------------------------

def _event_out(e: AgentEvent) -> dict:
    return {"id": e.id, "agent_id": e.agent_id, "kind": e.kind, "payload": e.payload,
            "cost": float(e.cost or 0.0),
            "created_at": e.created_at.isoformat() if e.created_at else None}


def _node(n: AgentNode) -> dict:
    return {"id": n.id, "tier": n.tier, "name": n.name, "status": n.status,
            "current_task": n.current_task, "last_run": n.last_run,
            "cost_today": n.cost_today, "children": [_node(c) for c in n.children]}


def _flatten(n: AgentNode) -> list[int]:
    out = [n.id]
    for c in n.children:
        out += _flatten(c)
    return out


def release(rows: list[int], last_id: int, gap: tuple[int, float] | None, now: float,
            grace: float) -> tuple[list[int], tuple[int, float] | None]:
    """Pure gap logic for the stream. `rows`: visible event ids > last_id, ascending.
    Returns (ids safe to emit now, gap state as (missing id, first seen)). Ids are released
    while contiguous; a missing id holds the rest back until it has been missing for `grace`
    seconds (then it is treated as a rolled-back id and skipped). The contiguity check
    continues after a skipped id: a later gap starts its own grace period."""
    out: list[int] = []
    expected = last_id + 1
    for rid in rows:
        if rid != expected:
            if gap is None or gap[0] != expected:
                return out, (expected, now)
            if now - gap[1] < grace:
                return out, gap
            gap = None                   # expired: skip the missing id(s), keep checking
        out.append(rid)
        expected = rid + 1
    return out, None if out or gap is None else gap


def settled_watermark(s: Session, grace: float) -> int:
    """Largest event id W such that every id <= W is either visible or an old (> `grace` s)
    permanent gap. Ids are assigned before commit, so a RECENT missing id may still be in
    flight: the watermark stops just below the lowest such gap. Reads one snapshot."""
    # Newest id older than the grace window, found by walking the primary key BACKWARDS and
    # stopping at the first match (cheap: only recent rows are visited, never the whole table).
    # Relies on created_at being the insert time (clock_timestamp default, migration 0009).
    row = s.execute(text(
        "select coalesce((select id from agent_events"
        " where created_at <= clock_timestamp() - make_interval(secs => :g)"
        " order by id desc limit 1), 0)"), {"g": grace}).scalar_one()
    base = int(row)
    recent = s.scalars(select(AgentEvent.id).where(AgentEvent.id > base)
                       .order_by(AgentEvent.id)).all()
    w = base
    for rid in recent:
        if rid != w + 1:
            break
        w = rid
    return w


# --- app factory ------------------------------------------------------------------------

def create_app(session_factory: Callable[[], Session] | None = None, *, poll_interval: float = 0.5,
               keepalive_interval: float = 15.0, gap_grace: float = 2.0,
               today: Callable[[], date] = date.today, config: dict | None = None) -> FastAPI:
    """Build the API. `session_factory` (default: ier.db.session.get_session, resolved on first
    use so importing needs no database) is called per request / per stream poll."""
    cfg = {**load_api_config(), **(config or {})}
    threshold = int(cfg["stale_after_days"])
    if session_factory is None:
        def session_factory() -> Session:
            from ier.db.session import get_session
            return get_session()

    app = FastAPI(title="India equity research API (read-only, local use only)")
    app.state.session_factory = session_factory
    app.add_middleware(CORSMiddleware, allow_origins=[DASHBOARD_ORIGIN], allow_methods=["GET", "POST"],
                       allow_headers=["Last-Event-ID", "Content-Type"])

    def db():
        s = session_factory()
        try:
            yield s
        finally:
            s.close()

    def fresh(as_of: date | None) -> dict:
        if as_of is None:
            return {"as_of": None, "age_days": None, "stale": True}
        age = (today() - as_of).days
        return {"as_of": as_of, "age_days": age, "stale": age > threshold}

    def meta(n: int) -> dict:
        return {"count": n, "stale_threshold_days": threshold, "stale_threshold_status": "PLACEHOLDER"}

    # latest scorecard / signal per instrument -------------------------------------------
    def latest_cards():
        return (select(Scorecard).ext(distinct_on(Scorecard.instrument_id))
                .order_by(Scorecard.instrument_id, Scorecard.run_date.desc(), Scorecard.id.desc())
                .subquery())

    def latest_signals():
        return (select(Signal).ext(distinct_on(Signal.instrument_id))
                .order_by(Signal.instrument_id, Signal.run_date.desc(), Signal.id.desc())
                .subquery())

    def stock_rows(s: Session, sector=None, industry=None):
        sc, sg = latest_cards(), latest_signals()
        q = (select(Instrument, sc.c.long_term_score, sc.c.short_term_score, sc.c.confidence,
                    sc.c.rules_version, sc.c.run_date, sc.c.sub_scores, sc.c.benefits, sc.c.risks,
                    sc.c.red_flags, sg.c.rating, sg.c.reasons, sg.c.rules_version.label("sig_rules"),
                    sg.c.run_date.label("sig_date"), sg.c.scorecard_id.label("sig_card"))
             .outerjoin(sc, sc.c.instrument_id == Instrument.id)
             .outerjoin(sg, sg.c.instrument_id == Instrument.id))
        if sector is not None:
            q = q.where(Instrument.sector == sector)
        if industry is not None:
            q = q.where(Instrument.industry == industry)
        return q, sc

    def stock_item(r) -> dict:
        i = r[0]
        return {"isin": i.isin, "name": i.name, "nse_code": i.nse_code, "bse_code": i.bse_code,
                "sector": i.sector, "industry": i.industry, "long_term_score": r.long_term_score,
                "short_term_score": r.short_term_score, "confidence": r.confidence,
                "rules_version": r.rules_version, **fresh(r.run_date), "rating": r.rating}

    def signal_out(r, conf) -> dict:
        i = r[0]
        return {"isin": i.isin, "name": i.name, "sector": i.sector, "industry": i.industry,
                "rating": r.rating, "reasons": list(r.reasons or []), "confidence": conf,
                "rules_version": r.sig_rules, **fresh(r.sig_date)}

    # agents ------------------------------------------------------------------------------
    @app.get("/agents/tree", response_model=S.TreeOut)
    def agents_tree(s: Session = Depends(db)):
        # one REPEATABLE READ snapshot for watermark and tree: the tree is exactly as new as
        # the watermark, and an in-flight lower id is never covered by it
        s.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        last = settled_watermark(s, gap_grace)
        try:
            root = tree(s)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from None
        return {"last_event_id": last, "tree": _node(root)}

    @app.get("/agents/summary", response_model=S.SummaryOut)
    def agents_summary(s: Session = Depends(db)):
        """Console header numbers. Declared before /agents/{agent_id} so 'summary' is not parsed
        as an id. 'Today' = since UTC midnight (same boundary as the tree's cost_today)."""
        s.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        midnight = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        queue_depth = s.scalar(select(func.count()).select_from(Job).where(Job.status == QUEUED))
        tok, secs = s.execute(select(
            func.coalesce(func.sum(func.coalesce(RunLog.prompt_tokens, 0)
                                   + func.coalesce(RunLog.completion_tokens, 0)), 0),
            func.coalesce(func.sum(RunLog.model_seconds), 0.0))
            .where(RunLog.created_at >= midnight)).one()
        failures = s.scalar(select(func.count()).select_from(AgentEvent)
                            .where(AgentEvent.kind == "failed", AgentEvent.created_at >= midnight))
        return {"queue_depth": int(queue_depth), "compute_tokens_today": int(tok),
                "compute_model_seconds_today": float(secs), "failures_today": int(failures),
                "last_event_id": settled_watermark(s, gap_grace)}

    @app.get("/agents/metrics", response_model=S.MetricsOut)
    def agents_metrics(s: Session = Depends(db)):
        """Chart series for the fleet dashboard (declared before /agents/{agent_id})."""
        s.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        now = datetime.now(timezone.utc)
        minute = now.replace(second=0, microsecond=0)
        hour = now.replace(minute=0, second=0, microsecond=0)
        m0, h0 = minute - timedelta(minutes=59), hour - timedelta(hours=23)
        mt = func.date_trunc("minute", AgentEvent.created_at)
        mins: dict[datetime, Counter] = {}
        for t, kind, n in s.execute(select(mt, AgentEvent.kind, func.count()).where(
                AgentEvent.created_at >= m0, AgentEvent.kind.in_(("started", "completed", "failed")))
                .group_by(mt, AgentEvent.kind)):
            mins.setdefault(t, Counter())[kind] = n
        events = []
        for i in range(60):
            t = m0 + timedelta(minutes=i)
            c = mins.get(t, Counter())
            events.append({"t": t, "started": c["started"], "completed": c["completed"], "failed": c["failed"]})
        ht = func.date_trunc("hour", RunLog.created_at)
        hrs = {t: (int(tok), float(sec)) for t, tok, sec in s.execute(
            select(ht, func.coalesce(func.sum(func.coalesce(RunLog.prompt_tokens, 0)
                                              + func.coalesce(RunLog.completion_tokens, 0)), 0),
                   func.coalesce(func.sum(RunLog.model_seconds), 0.0))
            .where(RunLog.created_at >= h0).group_by(ht))}
        tokens = [{"t": h0 + timedelta(hours=i), "tokens": hrs.get(h0 + timedelta(hours=i), (0, 0.0))[0],
                   "model_seconds": hrs.get(h0 + timedelta(hours=i), (0, 0.0))[1]} for i in range(24)]
        jobs = {"queued": 0, "running": 0, "done": 0, "failed": 0}
        for st, n in s.execute(select(Job.status, func.count()).group_by(Job.status)):
            jobs[st] = int(n)
        fails = s.scalars(select(AgentEvent).where(AgentEvent.kind == "failed")
                          .order_by(AgentEvent.id.desc()).limit(10)).all()
        return {"events_by_minute": events, "tokens_by_hour": tokens, "jobs_by_status": jobs,
                "recent_failures": [{"event_id": e.id, "agent_id": e.agent_id, "created_at": e.created_at,
                                     "error": (e.payload or {}).get("error") if isinstance(e.payload, dict) else None}
                                    for e in fails],
                "last_event_id": settled_watermark(s, gap_grace)}

    @app.get("/agents/activity", response_model=S.EventsOut)
    def agents_activity(limit: int = Query(400, ge=1, le=2000), s: Session = Depends(db)):
        """Recent lifecycle events across ALL agents, newest first (pipeline dashboard).
        Declared before /agents/{agent_id}."""
        rows = s.scalars(select(AgentEvent).where(AgentEvent.kind.in_(("queued", "started", "completed", "failed")))
                         .order_by(AgentEvent.id.desc()).limit(limit)).all()
        return {"events": [_event_out(e) for e in rows]}

    def agent_or_404(s: Session, agent_id: int) -> Agent:
        a = s.get(Agent, agent_id)
        if a is None:
            raise HTTPException(404, f"unknown agent id {agent_id}")
        return a

    @app.get("/agents/{agent_id}", response_model=S.AgentDetail)
    def agent_detail(agent_id: int, s: Session = Depends(db)):
        a = agent_or_404(s, agent_id)
        node = tree(s, agent_id)
        ids = _flatten(node)
        tokens = s.execute(select(
            func.coalesce(func.sum(RunLog.prompt_tokens), 0),
            func.coalesce(func.sum(RunLog.completion_tokens), 0),
            func.coalesce(func.sum(RunLog.model_seconds), 0.0)).where(RunLog.agent_id.in_(ids))).one()
        ev_cost = s.scalar(select(func.coalesce(func.sum(AgentEvent.cost), 0.0))
                           .where(AgentEvent.agent_id.in_(ids)))

        def latest(kind):
            return s.scalar(select(AgentEvent).where(AgentEvent.agent_id == agent_id,
                                                     AgentEvent.kind == kind)
                            .order_by(AgentEvent.id.desc()).limit(1))
        out, err = latest("output"), latest("failed")
        inst = s.get(Instrument, a.scope_instrument_id) if a.scope_instrument_id else None
        names = tier_names()
        return {
            "id": a.id, "tier": a.tier, "tier_name": names[a.tier - 1], "role": a.role,
            "name": node.name, "parent_id": a.parent_id, "scope_sector": a.scope_name,
            "isin": inst.isin if inst else None, "status": node.status,
            "current_task": node.current_task, "last_run": node.last_run,
            "rerunnable": a.role in RERUNNABLE_ROLES,
            "cost": {"cost_today": node.cost_today, "event_cost_total": float(ev_cost),
                     "prompt_tokens": int(tokens[0]), "completion_tokens": int(tokens[1]),
                     "total_tokens": int(tokens[0]) + int(tokens[1]),
                     "model_seconds": float(tokens[2])},
            "last_output": None if out is None else {
                "event_id": out.id, "created_at": out.created_at, "payload": out.payload},
            "last_error": None if err is None else {
                "event_id": err.id, "created_at": err.created_at, "payload": err.payload,
                "message": (err.payload or {}).get("error") if isinstance(err.payload, dict) else None},
        }

    @app.get("/agents/{agent_id}/events", response_model=S.EventsOut)
    def agent_events(agent_id: int, limit: int = Query(100, ge=1, le=500),
                     before_id: int | None = Query(None, ge=0), s: Session = Depends(db)):
        """Newest first; page backwards with before_id."""
        agent_or_404(s, agent_id)
        q = select(AgentEvent).where(AgentEvent.agent_id == agent_id)
        if before_id is not None:
            q = q.where(AgentEvent.id < before_id)
        rows = s.scalars(q.order_by(AgentEvent.id.desc()).limit(limit)).all()
        return {"events": [_event_out(e) for e in rows]}

    @app.post("/agents/{agent_id}/rerun", response_model=S.RerunOut)
    def agent_rerun(agent_id: int, s: Session = Depends(db)):
        a = agent_or_404(s, agent_id)
        if a.role not in RERUNNABLE_ROLES:
            raise HTTPException(409, f"agent {agent_id} (role {a.role!r}) cannot be rerun; "
                                     f"only {', '.join(RERUNNABLE_ROLES)} agents can")
        jobs = on_manual(s, agent_id)
        if jobs:
            job, already = jobs[0], False
            ev = s.scalar(select(AgentEvent.id).where(AgentEvent.agent_id == agent_id,
                                                      AgentEvent.kind == "queued")
                          .order_by(AgentEvent.id.desc()).limit(1))
        else:   # a manual job is already queued: report it instead of stacking another
            job = s.scalar(select(Job).where(Job.agent_id == agent_id, Job.trigger == MANUAL,
                                             Job.status == QUEUED).order_by(Job.id).limit(1))
            already, ev = True, None
        s.commit()
        return {"job_id": job.id, "agent_id": agent_id, "already_queued": already, "event_id": ev}

    # sources -----------------------------------------------------------------------------
    @app.get("/sources", response_model=S.SourcesOut)
    def sources(s: Session = Depends(db)):
        from ier.sources.service import list_sources
        out = list_sources(s)
        return {**out, "affects_score": False,
                "note": "Display only: no source changes any score until it has been calibrated."}

    @app.post("/sources/{source_id}/check", response_model=S.SourceOut)
    def source_check(source_id: str, s: Session = Depends(db)):
        """Run one source's connection check now (the Test button). Calls only the allow-listed
        hosts in ier.sources.connectors, GET only."""
        from ier.sources.registry import get_source, load_registry
        from ier.sources.service import check_source, list_sources
        reg = load_registry()
        src = get_source(reg, source_id)
        if src is None:
            raise HTTPException(404, f"unknown source {source_id!r}")
        check_source(s, src)
        s.commit()
        one = next(o for seg in list_sources(s, reg)["segments"] for o in seg["sources"] if o["id"] == source_id)
        return one

    # stream ------------------------------------------------------------------------------
    @app.get("/stream")
    async def stream(request: Request,
                     last_event_id_header: str | None = Header(None, alias="Last-Event-ID"),
                     last_event_id: str | None = Query(None)):
        raw = last_event_id_header if last_event_id_header not in (None, "") else last_event_id
        start: int | None = None
        if raw is not None and raw != "":
            try:
                start = int(raw)
                if not 0 <= start <= MAX_EVENT_ID:
                    raise ValueError
            except ValueError:
                raise HTTPException(422, f"Last-Event-ID must be an integer between 0 and {MAX_EVENT_ID}") from None

        def fetch(after: int) -> list[dict]:
            with session_factory() as s:       # short-lived: no transaction held across yields
                rows = s.scalars(select(AgentEvent).where(AgentEvent.id > after)
                                 .order_by(AgentEvent.id).limit(STREAM_BATCH)).all()
                return [_event_out(e) for e in rows]

        def current_max() -> int:
            with session_factory() as s:
                s.connection(execution_options={"isolation_level": "REPEATABLE READ"})
                return settled_watermark(s, gap_grace)

        # fixed before the response starts, so every event committed after the client sees the
        # response headers is delivered (no connect-time race)
        first = start if start is not None else await to_thread.run_sync(current_max)

        async def gen():
            last = first
            gap = None
            yield ": connected\n\n"
            beat = time.monotonic()
            while True:
                if await request.is_disconnected():
                    return
                rows = await to_thread.run_sync(fetch, last)
                now = time.monotonic()
                ids, gap = release([r["id"] for r in rows], last, gap, now, gap_grace)
                allowed = set(ids)
                for r in rows:
                    if r["id"] in allowed:
                        yield (f"id: {r['id']}\nevent: agent_event\n"
                               f"data: {json.dumps(r, default=str)}\n\n")
                        last = r["id"]
                if ids:
                    beat = now
                    if len(rows) >= STREAM_BATCH:
                        continue
                elif now - beat >= keepalive_interval:
                    yield ": keep-alive\n\n"
                    beat = now
                await asyncio.sleep(poll_interval)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # analyst -----------------------------------------------------------------------------
    @app.get("/analysts/{agent_id}", response_model=S.AnalystOut)
    def analyst(agent_id: int, s: Session = Depends(db)):
        a = agent_or_404(s, agent_id)
        company = a
        if a.role not in ("company",) and a.parent_id is not None and a.tier == tier_number("worker"):
            company = s.get(Agent, a.parent_id)          # a worker analyses its company's stock
        if company is None or company.scope_instrument_id is None:
            raise HTTPException(404, f"agent {agent_id} is not a company or worker analyst")
        inst = s.get(Instrument, company.scope_instrument_id)
        card = s.scalar(select(Scorecard).where(Scorecard.instrument_id == inst.id)
                        .order_by(Scorecard.run_date.desc(), Scorecard.id.desc()).limit(1))
        ids = _flatten(tree(s, company.id))     # the company's whole history, also for a worker
        logs = s.scalars(select(RunLog).where(RunLog.agent_id.in_(ids))
                         .order_by(RunLog.id.desc()).limit(100)).all()
        evidence = []
        if card is not None:
            for link in s.scalars(select(EvidenceLink).where(EvidenceLink.scorecard_id == card.id)
                                  .order_by(EvidenceLink.id)):
                fact = s.get(Fact, link.fact_id) if link.fact_id else None
                chunk = s.get(Chunk, link.chunk_id) if link.chunk_id else None
                sid = link.source_id or (fact.source_id if fact else None) or (
                    chunk.source_id if chunk else None)
                src = s.get(Source, sid) if sid else None
                evidence.append({
                    "claim_type": link.claim_type, "claim_ref": link.claim_ref,
                    "chunk_id": link.chunk_id,
                    "fact": None if fact is None else {
                        "id": fact.id, "metric": fact.metric, "value": fact.value,
                        "unit": fact.unit, "period": fact.period, "as_of": fact.period_end,
                        "conflict": bool(fact.conflict)},
                    "source": None if src is None else {
                        "id": src.id, "url": src.url, "type": src.type, "tier": src.tier,
                        "publish_date": src.publish_date, "fetched_at": src.fetch_date}})
        history = []
        for lg in logs:
            p, c = lg.prompt_tokens or 0, lg.completion_tokens or 0
            outs = lg.outputs if isinstance(lg.outputs, dict) else {}
            history.append({"run_log_id": lg.id, "run_id": lg.run_id, "role": lg.role,
                            "model_version": lg.model_version, "status": outs.get("status"),
                            "created_at": lg.created_at, "prompt_tokens": p,
                            "completion_tokens": c, "total_tokens": p + c,
                            "model_seconds": float(lg.model_seconds or 0.0)})
        findings = None if card is None else {
            "scorecard_id": card.id, "long_term_score": card.long_term_score,
            "short_term_score": card.short_term_score, "confidence": card.confidence,
            "sub_scores": card.sub_scores, "benefits": card.benefits, "risks": card.risks,
            "red_flags": card.red_flags}
        return {
            "agent_id": agent_id, "role": a.role, "name": inst.name, "isin": inst.isin,
            "findings": findings, "confidence": card.confidence if card else None,
            "rules_version": card.rules_version if card else None,
            "freshness": fresh(card.run_date if card else None),
            "evidence": evidence, "run_history": history,
            "rule_inputs": {
                "rules_version": card.rules_version if card else None,
                "scorecard_run_date": card.run_date if card else None,
                "scorecard_inputs": {} if card is None else {
                    "sub_scores": card.sub_scores, "long_term_score": card.long_term_score,
                    "short_term_score": card.short_term_score, "red_flags": card.red_flags,
                    "confidence": card.confidence},
                "latest_run_inputs": logs[0].inputs if logs else None}}

    # stocks / sectors / signals ----------------------------------------------------------
    @app.get("/stocks", response_model=S.StocksOut)
    def stocks(sector: str | None = None, industry: str | None = None,
               sort: str = Query("name"), s: Session = Depends(db)):
        desc = sort.startswith("-")
        key = sort[1:] if desc else sort
        if key not in SORT_KEYS:
            raise HTTPException(422, f"unknown sort key {key!r}; expected one of {list(SORT_KEYS)} "
                                     "(prefix '-' for descending)")
        q, sc = stock_rows(s, sector, industry)
        col = {"name": Instrument.name, "sector": Instrument.sector,
               "industry": Instrument.industry, "long_term_score": sc.c.long_term_score,
               "short_term_score": sc.c.short_term_score, "as_of": sc.c.run_date}[key]
        q = q.order_by((col.desc() if desc else col.asc()).nulls_last(), Instrument.name,
                       Instrument.isin)
        items = [stock_item(r) for r in s.execute(q)]
        return {"items": items, **meta(len(items))}

    @app.get("/stocks/{isin}", response_model=S.StockDetail)
    def stock(isin: str, s: Session = Depends(db)):
        q, _ = stock_rows(s)
        r = s.execute(q.where(Instrument.isin == isin)).first()
        if r is None:
            raise HTTPException(404, f"unknown ISIN {isin!r}")
        agent_id = s.scalar(select(Agent.id).where(Agent.role == "company",
                                                   Agent.scope_instrument_id == r[0].id)
                            .order_by(Agent.id).limit(1))
        sig = None
        if r.rating is not None:
            conf = s.scalar(select(Scorecard.confidence).where(Scorecard.id == r.sig_card))
            sig = signal_out(r, conf)
        return {**stock_item(r), "agent_id": agent_id, "sub_scores": r.sub_scores,
                "benefits": r.benefits, "risks": r.risks, "red_flags": r.red_flags, "signal": sig}

    @app.get("/stocks/{isin}/history", response_model=S.HistoryOut)
    def stock_history(isin: str, limit: int = Query(60, ge=1, le=365), s: Session = Depends(db)):
        inst = s.scalar(select(Instrument).where(Instrument.isin == isin))
        if inst is None:
            raise HTTPException(404, f"unknown ISIN {isin!r}")
        rows = s.execute(
            select(Scorecard.run_date, Scorecard.long_term_score, Scorecard.short_term_score,
                   Scorecard.confidence, Signal.rating)
            .outerjoin(Signal, Signal.scorecard_id == Scorecard.id)
            .where(Scorecard.instrument_id == inst.id)
            .order_by(Scorecard.run_date.desc()).limit(limit)).all()
        points = [{"run_date": r.run_date, "long_term_score": r.long_term_score,
                   "short_term_score": r.short_term_score, "confidence": r.confidence,
                   "rating": r.rating} for r in reversed(rows)]
        return {"isin": isin, "items": points, **meta(len(points))}

    @app.get("/sectors", response_model=S.SectorsOut)
    def sectors(s: Session = Depends(db)):
        q, _ = stock_rows(s)
        groups: dict[str, list] = {}
        for r in s.execute(q.where(Instrument.sector.is_not(None))):
            groups.setdefault(r[0].sector, []).append(r)
        items = []
        for name in sorted(groups):
            rows = groups[name]
            scored = [r for r in rows if r.run_date is not None]
            longs = [r.long_term_score for r in scored if r.long_term_score is not None]
            shorts = [r.short_term_score for r in scored if r.short_term_score is not None]
            confs = [r.confidence for r in scored if r.confidence]
            versions = sorted({r.rules_version for r in scored if r.rules_version})
            newest = max(scored, key=lambda r: r.run_date) if scored else None
            oldest = min((r.run_date for r in scored), default=None)
            items.append({
                "sector": name,
                "industries": sorted({r[0].industry for r in rows if r[0].industry}),
                "stock_count": len(rows), "scored_count": len(scored),
                "avg_long_term_score": sum(longs) / len(longs) if longs else None,
                "avg_short_term_score": sum(shorts) / len(shorts) if shorts else None,
                "confidence": min(confs, key=lambda c: CONF_RANK.get(c, -1)) if confs else None,
                "confidence_counts": dict(Counter(confs)),
                "signal_counts": dict(Counter(r.rating for r in rows if r.rating)),
                "rules_version": newest.rules_version if newest else None,
                "rules_versions": versions, **fresh(oldest)})
        return {"items": items, **meta(len(items))}

    @app.get("/signals", response_model=S.SignalsOut)
    def signals(sector: str | None = None, rating: RATING | None = None,
                s: Session = Depends(db)):
        q, _ = stock_rows(s, sector)
        confs = {}
        items = []
        rows = [r for r in s.execute(q) if r.rating is not None and (rating is None or r.rating == rating)]
        card_ids = {r.sig_card for r in rows if r.sig_card}
        if card_ids:
            confs = dict(s.execute(select(Scorecard.id, Scorecard.confidence)
                                   .where(Scorecard.id.in_(card_ids))).all())
        for r in sorted(rows, key=lambda r: (r[0].name, r[0].isin)):
            items.append(signal_out(r, confs.get(r.sig_card)))
        return {"items": items, **meta(len(items))}

    return app


app = create_app()
