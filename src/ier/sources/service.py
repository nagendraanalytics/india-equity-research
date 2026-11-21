"""Run a source check, store its state and series points, list sources for the API."""
from __future__ import annotations

import os
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from ier.db.models import Agent, Job, SeriesPoint, SourceState
from ier.runtime.queue import ACTIVE, enqueue, tier_number
from ier.sources import connectors
from ier.sources.registry import get_source, load_registry


def _local_dir() -> str:
    from ier.tools import exchange_filings
    return exchange_filings.load_config().get("local_dir") or ""


def check_source(session, src: dict, *, http=None, env=None, now=None, local_dir=None) -> SourceState:
    """Run the connector for `src`, upsert its SourceState and series points. Flushes only."""
    now = now or datetime.now(timezone.utc)
    ctx = connectors.Ctx(session=session, env=os.environ if env is None else env, now=now,
                         local_dir=_local_dir() if local_dir is None else local_dir, http=http)
    res = connectors.run_check(src, ctx)
    if res.points:
        rows = [{"series_id": s, "day": d, "value": v, "source_id": src["id"]} for s, d, v in res.points]
        for i in range(0, len(rows), 500):
            stmt = insert(SeriesPoint).values(rows[i:i + 500])
            session.execute(stmt.on_conflict_do_update(
                index_elements=["series_id", "day"],
                set_={"value": stmt.excluded.value, "source_id": stmt.excluded.source_id}))
    state = session.get(SourceState, src["id"]) or SourceState(source_id=src["id"], status="not_checked")
    state.status, state.detail, state.last_checked_at = res.status, res.detail[:500], now
    if res.status in (connectors.CONNECTED, connectors.STALE):
        state.last_ok_at, state.last_count = now, res.count
    state.latest = res.latest
    session.add(state)
    session.flush()
    return state


def list_sources(session, reg: dict | None = None) -> dict:
    reg = reg or load_registry()
    states = {s.source_id: s for s in session.scalars(select(SourceState))}
    out = []
    for src in reg["sources"]:
        st = states.get(src["id"])
        out.append({"id": src["id"], "segment": src["segment"], "name": src["name"], "kind": src["kind"],
                    "terms": src.get("terms"), "refresh_minutes": src.get("refresh_minutes", 0),
                    "status": st.status if st else "not_checked", "detail": st.detail if st else None,
                    "last_checked_at": st.last_checked_at if st else None,
                    "last_ok_at": st.last_ok_at if st else None,
                    "count": st.last_count if st else None, "latest": st.latest if st else None})
    segs = []
    for sid, meta in reg["segments"].items():
        mine = [o for o in out if o["segment"] == sid]
        connected = sum(1 for o in mine if o["status"] == connectors.CONNECTED)
        segs.append({"id": sid, "name": meta["name"], "note": meta.get("note"),
                     "connected": connected, "total": len(mine), "sources": mine})
    return {"segments": segs, "connected": sum(s["connected"] for s in segs), "total": len(out)}


# --- agents ------------------------------------------------------------------------------

def seed_source_agents(session, reg: dict | None = None) -> int:
    """segment controllers under the master and one source agent per source; idempotent."""
    reg = reg or load_registry()
    master = session.scalar(select(Agent).where(Agent.role == "master"))
    if master is None:
        raise ValueError("no master agent: run the seed command for a macro sector first")
    seg_tier, src_tier = tier_number("segment"), tier_number("source")
    existing = {(a.tier, a.parent_id, a.scope_name): a for a in session.scalars(
        select(Agent).where(Agent.tier.in_([seg_tier, src_tier])))}
    created = 0
    for sid in reg["segments"]:
        ctrl = existing.get((seg_tier, master.id, sid))
        if ctrl is None:
            ctrl = Agent(tier=seg_tier, parent_id=master.id, role="segment", scope_name=sid)
            session.add(ctrl)
            session.flush()
            created += 1
        for src in (s for s in reg["sources"] if s["segment"] == sid):
            if (src_tier, ctrl.id, src["id"]) not in existing:
                session.add(Agent(tier=src_tier, parent_id=ctrl.id, role="source", scope_name=src["id"]))
                created += 1
    session.flush()
    return created


def schedule_due(session, now: datetime | None = None, reg: dict | None = None) -> int:
    """Queue a check for every source whose refresh interval has passed (and no check is
    already queued or running), then one controller summary job per segment that got any.
    Flushes; never commits. Returns jobs queued."""
    from datetime import timedelta
    now = now or datetime.now(timezone.utc)
    reg = reg or load_registry()
    src_tier, seg_tier = tier_number("source"), tier_number("segment")
    agents = {a.scope_name: a for a in session.scalars(select(Agent).where(Agent.tier == src_tier))}
    ctrls = {a.scope_name: a for a in session.scalars(select(Agent).where(Agent.tier == seg_tier))}
    states = {s.source_id: s for s in session.scalars(select(SourceState))}
    queued = 0
    for sid in reg["segments"]:
        last_job = None
        for src in (s for s in reg["sources"] if s["segment"] == sid):
            every, agent = int(src.get("refresh_minutes", 0) or 0), agents.get(src["id"])
            if every <= 0 or agent is None:
                continue
            st = states.get(src["id"])
            if st and st.last_checked_at and now - st.last_checked_at < timedelta(minutes=every):
                continue
            busy = session.scalar(select(Job.id).where(Job.agent_id == agent.id, Job.status.in_(ACTIVE)).limit(1))
            if busy is not None:
                continue
            last_job = enqueue(session, agent.id, "schedule")
            queued += 1
        ctrl = ctrls.get(sid)
        if last_job is not None and ctrl is not None:
            busy = session.scalar(select(Job.id).where(Job.agent_id == ctrl.id, Job.status.in_(ACTIVE)).limit(1))
            if busy is None:
                enqueue(session, ctrl.id, "schedule", depends_on=last_job.id)
                queued += 1
    return queued
