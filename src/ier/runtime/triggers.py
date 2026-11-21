"""Change-driven triggers (spec 4.2a). All idempotent; they flush, never commit.

Only a job that is still QUEUED at write time absorbs a repeat trigger (same agent + trigger,
+ payload key where relevant; conditional UPDATE, see _absorb); where the payload matters it
is refreshed to the latest values. A RUNNING job never
absorbs one: it may already have read the old inputs, so a new queued job is created and runs
after it (tier concurrency applies as normal). Unchanged inputs create no job.
Each trigger returns only the jobs it newly created.
"""
from __future__ import annotations

from datetime import datetime
from typing import Iterable

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from ier.db.models import Agent, Document, Job, Source
from ier.runtime.queue import FAILED, QUEUED, enqueue, tier_number

NEW_SOURCE, MARKET_EVENT, SCHEDULE, MANUAL = "new_source", "market_event", "schedule", "manual"
NEW_FILE = "new_file"


def _queued(session: Session, agent_id: int, trigger: str, *where) -> Job | None:
    """Candidate queued job to absorb a repeat trigger (unlocked read; see _absorb)."""
    return session.execute(
        select(Job).where(Job.agent_id == agent_id, Job.trigger == trigger,
                          Job.status == QUEUED, *where).order_by(Job.id).limit(1)
    ).scalar_one_or_none()


def _absorb(session: Session, job: Job | None, payload: dict | None = None) -> bool:
    """Atomically let a still-QUEUED job absorb a trigger: UPDATE ... WHERE id AND
    status='queued' (optionally refreshing its payload). False if there was no candidate or a
    worker claimed it since the lookup -- the caller then creates a new queued job.

    If a claimant holds the row lock (uncommitted claim), this UPDATE waits for it and then
    re-checks status, so it never writes into a job that is running. Once it matches, the row
    stays locked until the trigger's transaction ends, so claim_next skips it meanwhile.
    """
    if job is None:
        return False
    stmt = update(Job).where(Job.id == job.id, Job.status == QUEUED)
    stmt = stmt.values(payload=dict(payload)) if payload is not None else stmt.values(status=QUEUED)
    matched = session.execute(stmt.execution_options(synchronize_session=False)).rowcount == 1
    session.expire(job)  # identity-map copy may be stale either way
    return matched


def on_new_source(session: Session, source_id: int) -> list[Job]:
    """Jobs for the company agents (Document.instrument_id) and sector agents (Document.sector)
    whose input this source is, unless already processed at the same content hash."""
    src = session.get(Source, source_id)
    if src is None or src.content_hash is None:
        return []
    docs = session.execute(
        select(Document.instrument_id, Document.sector).where(Document.source_id == source_id)
    ).all()
    instruments = {i for i, _ in docs if i is not None}
    sectors = {s for _, s in docs if s is not None}
    if not instruments and not sectors:
        return []
    agents = session.execute(
        select(Agent).where(or_(
            (Agent.tier == tier_number("company")) & (Agent.role == "company")
            & Agent.scope_instrument_id.in_(instruments),
            (Agent.tier == tier_number("sector")) & Agent.scope_name.in_(sectors),
        )).order_by(Agent.id)
    ).scalars().all()
    payload_source = Job.payload["source_id"].as_integer() == source_id
    payload = {"source_id": source_id, "content_hash": src.content_hash}
    jobs = []
    for agent in agents:
        # unchanged-source rule: this agent already has (or had) a non-failed job for this
        # same source_id at this same content hash
        seen = session.execute(
            select(Job.id).where(
                Job.agent_id == agent.id, Job.trigger == NEW_SOURCE, Job.status != FAILED,
                payload_source, Job.payload["content_hash"].as_string() == src.content_hash,
            ).limit(1)
        ).first()
        if seen is not None:
            continue
        if _absorb(session, _queued(session, agent.id, NEW_SOURCE, payload_source), payload):
            continue
        jobs.append(enqueue(session, agent.id, NEW_SOURCE, payload=dict(payload)))
    return jobs


def on_market_event(session: Session, instrument_id: int, kind: str, magnitude: float,
                    threshold: float) -> list[Job]:
    """Jobs for the instrument's company agents when |magnitude| >= threshold."""
    if abs(magnitude) < threshold:
        return []
    agents = session.execute(
        select(Agent.id).where(Agent.tier == tier_number("company"), Agent.role == "company",
                               Agent.scope_instrument_id == instrument_id).order_by(Agent.id)
    ).scalars().all()
    same_kind = Job.payload["kind"].as_string() == kind
    payload = {"instrument_id": instrument_id, "kind": kind,
               "magnitude": magnitude, "threshold": threshold}
    jobs = []
    for aid in agents:
        # a queued job takes the latest event; if it was claimed meanwhile, queue a new one
        if not _absorb(session, _queued(session, aid, MARKET_EVENT, same_kind), payload):
            jobs.append(enqueue(session, aid, MARKET_EVENT, payload=dict(payload)))
    return jobs


def on_schedule(session: Session, agent_ids: Iterable[int], when: datetime) -> list[Job]:
    """Scheduled refresh at `when` (timezone-aware) for each agent without a queued scheduled
    job; an existing queued one keeps its own time."""
    if when.tzinfo is None or when.utcoffset() is None:
        raise ValueError("on_schedule: `when` must be timezone-aware")
    return [enqueue(session, aid, SCHEDULE, scheduled_at=when)
            for aid in agent_ids if not _absorb(session, _queued(session, aid, SCHEDULE))]


MANUAL_PAYLOAD = {"force": True}


def on_manual(session: Session, agent_id: int) -> list[Job]:
    """Manual re-run; empty if one is already queued (a running one does not absorb it: the
    request may have been prompted by something that run has not seen). The job carries
    payload {"force": true}: a person asked for it, so the unchanged-inputs skip never applies
    (company_job_handler honours it)."""
    if _absorb(session, _queued(session, agent_id, MANUAL), dict(MANUAL_PAYLOAD)):
        return []
    return [enqueue(session, agent_id, MANUAL, payload=dict(MANUAL_PAYLOAD))]


def on_files_changed(session: Session, instrument_id: int, fingerprint: str) -> list[Job]:
    """The instrument's local filings folder changed: queue its company agent. A still-queued
    job takes the latest fingerprint instead of a second job. No force flag: the company run's
    own unchanged-inputs check still skips a run whose filings it has already processed."""
    agents = session.execute(
        select(Agent.id).where(Agent.tier == tier_number("company"), Agent.role == "company",
                               Agent.scope_instrument_id == instrument_id).order_by(Agent.id)
    ).scalars().all()
    payload = {"instrument_id": instrument_id, "fingerprint": fingerprint}
    jobs = []
    for aid in agents:
        if not _absorb(session, _queued(session, aid, NEW_FILE), payload):
            jobs.append(enqueue(session, aid, NEW_FILE, payload=dict(payload)))
    return jobs
