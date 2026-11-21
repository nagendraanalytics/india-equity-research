"""Job handlers for source agents (run one connection check) and segment controllers
(summarise their sources). No model calls, no score changes."""
from __future__ import annotations

from sqlalchemy import select

from ier.db.models import Agent, SourceState
from ier.runtime.events import emit
from ier.sources import connectors
from ier.sources.registry import get_source, load_registry
from ier.sources.service import check_source


class SourceCheckFailed(RuntimeError):
    pass


def run_source_agent(session, job) -> None:
    agent = session.get(Agent, job.agent_id)
    src = get_source(load_registry(), agent.scope_name or "")
    if src is None:
        raise SourceCheckFailed(f"source {agent.scope_name!r} is no longer in config/sources.yaml")
    state = check_source(session, src)
    emit(session, agent.id, "output", {"source": src["id"], "status": state.status, "detail": state.detail,
                                       "count": state.last_count}, own_transaction=False)
    if state.status == connectors.ERROR:
        raise SourceCheckFailed(state.detail or "check failed")


def run_segment_agent(session, job) -> None:
    ctrl = session.get(Agent, job.agent_id)
    ids = [a.scope_name for a in session.scalars(select(Agent).where(Agent.parent_id == ctrl.id))]
    states = {s.source_id: s for s in session.scalars(select(SourceState).where(SourceState.source_id.in_(ids)))}
    rows = [{"id": i, "status": states[i].status if i in states else "not_checked"} for i in ids]
    connected = sum(1 for r in rows if r["status"] == connectors.CONNECTED)
    emit(session, ctrl.id, "output", {"segment": ctrl.scope_name, "connected": connected, "of": len(rows),
                                      "sources": rows, "affects_score": False}, own_transaction=False)
