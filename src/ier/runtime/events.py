"""Agent registry, append-only event log and status tree (spec 4.2a, live console).

`emit` is the only write path to agent_events. The log is append-only (ORM listeners in
ier.db.models plus a Postgres trigger, migration 0007). Tier, parent and scope of an event
are derived by joining Agent; an optional run id travels in payload["run_id"].
`emit` commits each event in its own short transaction on a separate connection whenever the
caller's session is bound to an Engine, so an event is visible as soon as its id is assigned
even while a long job transaction is still open (the live stream relies on this). Events of a
job that later rolls back stay in the log: it records what happened. If the session is bound
to a Connection (tests that wrap everything in one outer transaction) or the agent row is not
yet committed, or own_transaction=False, the event joins the caller's transaction.
The other functions here flush and never commit.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import aliased
from sqlalchemy.engine import Engine
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.orm import Session

from ier.db.models import Agent, AgentEvent, Instrument, Job
from ier.runtime.queue import QUEUED, SETTLED, _cfg, tier_number
from ier.universe.filter import build_universe

EVENT_KINDS = ("queued", "started", "step", "output", "failed", "completed")
_STATUS_KINDS = ("queued", "started", "completed", "failed")
_RUN_KINDS = ("completed", "failed")


def emit(session: Session, agent_id: int, kind: str, payload: dict | None,
         cost: float = 0.0, *, own_transaction: bool = True) -> AgentEvent:
    """Append one event, committed independently of `session` (see module docstring) when
    possible, else flushed in `session`. ValueError for an unknown kind, a negative or
    non-finite cost, or an unknown agent_id.

    own_transaction=False keeps the event in the caller's transaction. Use it for an event that
    must live or die with other rows the caller is writing: `queued` (with its Job) and a job's
    terminal `completed`/`failed` (with the job status). Otherwise an independently committed
    status event could outlive a rollback and the status tree would show a job that is gone.

    Connections: an independent event borrows a SECOND pooled connection while the caller's
    job transaction holds its own, so size the pool for 2 x concurrent workers (plus API
    requests/streams), or workers can stall waiting for a connection. The short
    transaction-per-event also assumes nothing ever locks agent rows FOR UPDATE for long.
    """
    if kind not in EVENT_KINDS:
        raise ValueError(f"unknown event kind {kind!r}; expected one of {EVENT_KINDS}")
    try:
        cost = float(cost)
    except (TypeError, ValueError):
        raise ValueError(f"cost must be a number, got {cost!r}") from None
    if not math.isfinite(cost) or cost < 0:
        raise ValueError(f"cost must be finite and >= 0, got {cost!r}")
    if session.get(Agent, agent_id) is None:
        raise ValueError(f"unknown agent_id {agent_id!r}")
    body = payload if payload is not None else {}
    event = _write(session, agent_id, kind, body, cost, own_transaction)
    if kind == "started":
        # the worker sets this list around a handler to learn which `started` events the job
        # emitted, so it can close exactly those if the job rolls back (see runtime.worker)
        track = session.info.get("started_events")
        if track is not None:
            track.append((agent_id, event.id))
    return event


def _write(session: Session, agent_id: int, kind: str, body, cost: float,
           own_transaction: bool) -> AgentEvent:
    bind = session.get_bind()
    if own_transaction and isinstance(bind, Engine):
        with Session(bind, expire_on_commit=False) as own:
            if own.get(Agent, agent_id) is not None:   # else agent is uncommitted: join caller
                event = AgentEvent(agent_id=agent_id, kind=kind, payload=body, cost=cost)
                own.add(event)
                own.commit()
                return event
    event = AgentEvent(agent_id=agent_id, kind=kind, payload=body, cost=cost)
    session.add(event)
    session.flush()
    return event


# --- seeding ---------------------------------------------------------------------------

RETIRED_COMPANY_ROLE = "retired_company"


def seed_agents(session: Session, macro_sector: str) -> int:
    """Create the agent hierarchy for one macro sector; idempotent. Returns agents created.

    master -> macro_sector -> sector -> industry -> company -> workers. Sectors/industries
    come from the universe (build_universe); instruments lacking a sector or industry cannot
    be placed in the taxonomy and are skipped.

    One instrument has exactly ONE active company agent. If the instrument was reclassified
    (another industry/sector), its existing company agent is re-parented to the new industry
    (workers, events and history move with it); no second one is created. Duplicates left by
    older versions are retired: role "retired_company", parent_id NULL (kept for history, out
    of the tree, never planned or triggered); the oldest agent stays active.
    """
    cfg = _cfg(None)
    worker_roles = list(cfg.get("worker_roles", []))
    company_tier = tier_number("company")
    existing: dict[tuple, Agent] = {}
    companies: dict[int, list[Agent]] = {}
    for a in session.scalars(select(Agent).order_by(Agent.id)):
        if a.role == "company" and a.tier == company_tier and a.scope_instrument_id is not None:
            companies.setdefault(a.scope_instrument_id, []).append(a)
        else:
            existing[(a.tier, a.parent_id, a.role, a.scope_name, a.scope_instrument_id)] = a
    created = 0

    def ensure(tier: str, parent: Agent | None, role: str, scope_name: str | None = None,
               instrument_id: int | None = None) -> Agent:
        nonlocal created
        no = tier_number(tier)
        key = (no, parent.id if parent else None, role, scope_name, instrument_id)
        agent = existing.get(key)
        if agent is None:
            agent = Agent(tier=no, parent_id=key[1], role=role, scope_name=scope_name,
                          scope_instrument_id=instrument_id)
            session.add(agent)
            session.flush()
            existing[key] = agent
            created += 1
        return agent

    def ensure_company(industry: Agent, instrument_id: int) -> Agent:
        nonlocal created
        agents = companies.get(instrument_id, [])
        if not agents:
            agent = Agent(tier=company_tier, parent_id=industry.id, role="company",
                          scope_instrument_id=instrument_id)
            session.add(agent)
            session.flush()
            companies[instrument_id] = [agent]
            created += 1
            return agent
        agent, *dups = agents
        for dup in dups:
            dup.role, dup.parent_id = RETIRED_COMPANY_ROLE, None
        if agent.parent_id != industry.id:
            agent.parent_id = industry.id     # reclassified: move, keep history
        companies[instrument_id] = [agent]
        session.flush()
        return agent

    master = ensure("master", None, "master")
    macro = ensure("macro_sector", master, "macro_sector", macro_sector)
    for inst in build_universe(session, macro_sector):
        if not inst.sector or not inst.industry:
            continue
        sector = ensure("sector", macro, "sector", inst.sector)
        industry = ensure("industry", sector, "industry", inst.industry)
        company = ensure_company(industry, inst.id)
        for role in worker_roles:
            ensure("worker", company, role)
    return created


# --- status tree -----------------------------------------------------------------------

@dataclass(frozen=True)
class AgentNode:
    id: int
    tier: int
    name: str
    status: str
    current_task: str | None
    last_run: datetime | None
    cost_today: float
    children: tuple["AgentNode", ...] = ()


_OWN_STATUS = {"queued": "queued", "started": "running", "completed": "completed",
               "failed": "failed"}


def tree(session: Session, root_id: int | None = None, *, now: datetime | None = None) -> AgentNode:
    """Status tree under `root_id` (default: the master). Five queries regardless of size.

    Own status comes from the agent's latest queued/started/completed/failed event (ordered
    by event id; created_at ties inside one transaction). An agent whose own status is queued
    but whose queued job(s) all wait on a parent job that is not settled yet (depends_on a
    queued/running job) is `blocked` instead: it cannot start until the parent finishes. A
    node is `running` if it or any descendant is; else `failed` if its own latest event failed
    (that node only); else `degraded` if a descendant failed; else `blocked` if its own status
    is blocked; else `queued` if it is queued or a descendant is queued or blocked; else
    `completed` if it or a descendant completed; else `idle`. After a budget halt the worker
    closes aborted worker agents as `failed`, so their company agent and ancestors read
    `degraded` until the next successful run (intended). current_task is set only while
    the agent's latest status event is `started`. cost_today sums events since UTC
    midnight of `now` (injectable for tests), descendants included.
    """
    now = now or datetime.now(timezone.utc)
    midnight = now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

    rows = session.execute(
        select(Agent, Instrument.name).outerjoin(Instrument, Instrument.id == Agent.scope_instrument_id)
        .order_by(Agent.id)
    ).all()
    agents = {a.id: (a, iname) for a, iname in rows}
    kids: dict[int | None, list[int]] = {}
    for a, _ in rows:
        kids.setdefault(a.parent_id, []).append(a.id)
    if root_id is None:
        masters = [a.id for a, _ in rows if a.parent_id is None and a.tier == tier_number("master")]
        if not masters:
            raise ValueError("no master agent; run seed_agents first")
        root_id = masters[0]
    elif root_id not in agents:
        raise ValueError(f"unknown agent id {root_id!r}")

    latest = session.execute(
        select(AgentEvent.agent_id, AgentEvent.kind, AgentEvent.payload)
        .ext(distinct_on(AgentEvent.agent_id))
        .where(AgentEvent.kind.in_(_STATUS_KINDS))
        .order_by(AgentEvent.agent_id, AgentEvent.id.desc())
    ).all()
    own = {r.agent_id: (r.kind, r.payload) for r in latest}
    parent = aliased(Job)
    waiting = select(parent.id).where(parent.id == Job.depends_on,
                                      parent.status.not_in(SETTLED)).exists()
    queued_jobs = session.execute(
        select(Job.agent_id, waiting.label("waiting")).where(Job.status == QUEUED)).all()
    runnable_agents = {r.agent_id for r in queued_jobs if not r.waiting}
    blocked = {r.agent_id for r in queued_jobs if r.waiting} - runnable_agents
    last_runs = dict(session.execute(
        select(AgentEvent.agent_id, func.max(AgentEvent.created_at))
        .where(AgentEvent.kind.in_(_RUN_KINDS)).group_by(AgentEvent.agent_id)
    ).all())
    costs = dict(session.execute(
        select(AgentEvent.agent_id, func.coalesce(func.sum(AgentEvent.cost), 0.0))
        .where(AgentEvent.created_at >= midnight)
        .group_by(AgentEvent.agent_id)
    ).all())

    def build(aid: int, seen: frozenset) -> tuple[AgentNode, set[str]]:
        agent, iname = agents[aid]
        children, desc_statuses = [], set()
        last_run, cost = last_runs.get(aid), float(costs.get(aid, 0.0))
        for cid in kids.get(aid, ()):
            if cid in seen:
                continue
            child, child_statuses = build(cid, seen | {cid})
            children.append(child)
            desc_statuses |= child_statuses | {child_own(cid)}
            cost += child.cost_today
            if child.last_run and (last_run is None or child.last_run > last_run):
                last_run = child.last_run
        kind, payload = own.get(aid, (None, None))
        own_status = child_own(aid)
        if own_status == "running" or "running" in desc_statuses:
            status = "running"
        elif own_status == "failed":
            status = "failed"
        elif "failed" in desc_statuses:
            status = "degraded"
        elif own_status == "blocked":
            status = "blocked"
        elif own_status == "queued" or desc_statuses & {"queued", "blocked"}:
            status = "queued"
        elif own_status == "completed" or "completed" in desc_statuses:
            status = "completed"
        else:
            status = "idle"
        task = None
        if kind == "started":
            p = payload if isinstance(payload, dict) else {}
            task = p.get("task") or (str(p["job_id"]) if p.get("job_id") is not None else None)
        if agent.scope_instrument_id is not None and iname:
            name = iname
        else:
            name = agent.scope_name or agent.role
        return AgentNode(aid, agent.tier, name, status, task, last_run, cost, tuple(children)), desc_statuses

    def child_own(cid: int) -> str:
        status = _OWN_STATUS.get(own.get(cid, (None, None))[0], "idle")
        return "blocked" if status == "queued" and cid in blocked else status

    return build(root_id, frozenset({root_id}))[0]
