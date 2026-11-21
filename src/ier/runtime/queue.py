"""Postgres-backed job queue (spec 4.2a).

Status values: queued -> running -> done | failed. Claiming uses
SELECT ... FOR UPDATE SKIP LOCKED so concurrent workers on separate sessions never get the
same job. Every function here flushes and never commits: the caller owns the transaction.
A worker should commit right after claim_next so the claim (status running, claimed_at) is
visible; requeue_stale returns claims left behind by a crashed worker.

Event delivery guarantee: the `queued` event written by enqueue/release (and the worker's
terminal events) joins the caller's transaction, so it becomes visible only when that
transaction commits. The live stream delivers it only if the commit happens within `gap_grace`
(default 2 s) of the insert; a longer transaction can be skipped by a connected stream. Clients
(the dashboard, Task 16) must therefore resync from GET /agents/tree on every reconnect AND
periodically, not rely on the stream alone. This applies to every trigger in
ier.runtime.triggers and to plan_run in ier.graph.company, which all enqueue through here.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import DateTime, func, literal, or_, select, update
from sqlalchemy.orm import Session, aliased

from ier.db.models import Agent, Job

ROOT = Path(__file__).resolve().parents[3]
CONFIG_PATH = ROOT / "config" / "runtime.yaml"

QUEUED, RUNNING, DONE, FAILED = "queued", "running", "done", "failed"
ACTIVE = (QUEUED, RUNNING)
# A dependent job may run once its parent is settled: done, or failed for good. A parent's
# output (e.g. the sector tailwind) is an optional input: the dependent runs without it and the
# rules engine treats it as missing (or uses the last stored sector view), so one failed
# sector job never leaves its industries and companies queued forever.
SETTLED = (DONE, FAILED)
STALE_DEFAULTS = {"after_seconds": 7200, "check_interval_seconds": 300}
MAX_ERROR_CHARS = 4000

_config: dict | None = None


def load_config(path: Path = CONFIG_PATH) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def _cfg(config: dict | None) -> dict:
    global _config
    if config is not None:
        return config
    if _config is None:
        _config = load_config()
    return _config


def tier_names(config: dict | None = None) -> list[str]:
    return list(_cfg(config)["tiers"])


def tier_number(name: str, config: dict | None = None) -> int:
    """Agent.tier integer (1-based, spec order) for a tier name; ValueError if unknown."""
    names = tier_names(config)
    if name not in names:
        raise ValueError(f"unknown tier {name!r}; expected one of {names}")
    return names.index(name) + 1


def _now() -> datetime:
    return datetime.now(timezone.utc)


def enqueue(
    session: Session,
    agent_id: int,
    trigger: str,
    scheduled_at: datetime | None = None,
    *,
    payload: Any = None,
    depends_on: int | None = None,
) -> Job:
    """Queue a job. `scheduled_at` must be timezone-aware (ValueError if naive); None means
    due now and is stored as the current UTC time, so claim order is by due time and a
    backed-off retry is not starved by jobs enqueued after it became due."""
    if scheduled_at is None:
        scheduled_at = _now()
    elif scheduled_at.tzinfo is None or scheduled_at.utcoffset() is None:
        raise ValueError("enqueue: scheduled_at must be timezone-aware (naive datetime given)")
    job = Job(agent_id=agent_id, trigger=trigger, status=QUEUED, attempts=0,
              scheduled_at=scheduled_at, payload=payload, depends_on=depends_on)
    session.add(job)
    session.flush()
    from ier.runtime.events import emit  # local: events imports this module's tier helpers
    emit(session, agent_id, "queued", {"job_id": job.id, "trigger": trigger},
         own_transaction=False)   # durable only together with the Job row
    return job


def claim_next(session: Session, tier: str) -> Job | None:
    """Claim the next runnable job for agents of `tier` (row-locked, one claimant only).

    Runnable: queued, scheduled_at null or due, depends_on null or pointing at a settled
    (done or failed) job, and the agent has no running job (one job per agent at a time).
    Order: coalesce(scheduled_at, '-infinity'), then id -- i.e. oldest due time first.

    The job AND its agent row are locked (FOR NO KEY UPDATE ... SKIP LOCKED) until the
    claimant commits, so two workers cannot both claim jobs of one agent before either claim
    is visible; NO KEY UPDATE does not block event inserts (their FK takes KEY SHARE).
    """
    tier_no = tier_number(tier)
    now = _now()
    parent = aliased(Job)
    parent_done = select(parent.id).where(parent.id == Job.depends_on, parent.status.in_(SETTLED))
    sibling = aliased(Job)
    agent_busy = select(sibling.id).where(sibling.agent_id == Job.agent_id,
                                          sibling.status == RUNNING)
    stmt = (
        select(Job)
        .join(Agent, Agent.id == Job.agent_id)
        .where(
            Agent.tier == tier_no,
            Job.status == QUEUED,
            or_(Job.scheduled_at.is_(None), Job.scheduled_at <= now),
            or_(Job.depends_on.is_(None), parent_done.exists()),
            ~agent_busy.exists(),
        )
        # due time, then id; NULL (rows written without enqueue) counts as -infinity
        .order_by(func.coalesce(Job.scheduled_at, literal("-infinity").cast(DateTime(timezone=True))), Job.id)
        .limit(1)
        .with_for_update(skip_locked=True, key_share=True, of=(Job, Agent))
    )
    job = session.execute(stmt).scalar_one_or_none()
    if job is None:
        return None
    # The agent_busy filter above used this statement's snapshot (READ COMMITTED), which may
    # predate another worker's just-committed claim of a sibling job; row locks alone do not
    # re-evaluate it. Re-check in a NEW statement now that we hold the locks (fresh snapshot).
    # Returning None leaves the locks to the caller's commit/rollback, as for any empty claim.
    busy = session.scalar(select(Job.id).where(Job.agent_id == job.agent_id,
                                               Job.status == RUNNING, Job.id != job.id).limit(1))
    if busy is not None:
        return None
    job.status = RUNNING
    job.claimed_at = now
    session.flush()
    return job


def complete(session: Session, job: Job) -> None:
    job.status = DONE
    session.flush()


def fail(session: Session, job: Job, error: Any, *, config: dict | None = None) -> None:
    """Count a failed attempt; requeue with exponential backoff or mark failed at the cap."""
    retry = _cfg(config)["retry"]
    job.attempts = (job.attempts or 0) + 1
    job.last_error = str(error)[:MAX_ERROR_CHARS]
    job.claimed_at = None
    if job.attempts < int(retry["max_attempts"]):
        delay = float(retry["backoff_base_seconds"]) * 2 ** (job.attempts - 1)
        job.status = QUEUED
        job.scheduled_at = _now() + timedelta(seconds=delay)
    else:
        job.status = FAILED
    session.flush()


def fail_now(session: Session, job: Job, error: Any) -> None:
    """Count a failed attempt and mark the job failed with no retry (e.g. its own per-run
    compute cap is used up: a retry would hit the same cap)."""
    job.attempts = (job.attempts or 0) + 1
    job.last_error = str(error)[:MAX_ERROR_CHARS]
    job.claimed_at = None
    job.status = FAILED
    session.flush()


def next_utc_midnight(now: datetime | None = None) -> datetime:
    now = (now or _now()).astimezone(timezone.utc)
    return (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)


def release(session: Session, job: Job, *, until_next_midnight: bool = False) -> None:
    """Return a claimed job to the queue (e.g. on BudgetExceeded); attempts unchanged.
    until_next_midnight=True (per-day cap reached) makes it due at the next UTC midnight, when
    the daily budget resets, instead of at its original due time (which would make it the first
    job claimed again and refused again)."""
    job.status = QUEUED
    job.claimed_at = None
    payload = {"job_id": job.id, "reason": "budget_halt"}
    if until_next_midnight:
        job.scheduled_at = next_utc_midnight()
        payload["scheduled_at"] = job.scheduled_at.isoformat()
    session.flush()
    from ier.runtime.events import emit
    emit(session, job.agent_id, "queued", payload, own_transaction=False)


def stale_settings(config: dict | None = None) -> tuple[timedelta, float]:
    """(older_than, check interval seconds) from runtime config `stale` (PLACEHOLDER defaults:
    a job claimed 2 h ago is presumed lost; checked every 5 min). older_than must exceed the
    longest legitimate job (the per-run model-seconds cap is 30 min), or a slow job is run
    twice."""
    st = {**STALE_DEFAULTS, **((config or {}).get("stale") or _cfg(None).get("stale") or {})}
    return timedelta(seconds=float(st["after_seconds"])), float(st["check_interval_seconds"])


def requeue_stale(session: Session, older_than: timedelta, *, config: dict | None = None) -> int:
    """Recover running jobs claimed more than `older_than` ago (their worker crashed or lost
    its connection). Each counts a failed attempt: below retry.max_attempts it goes back to
    queued (event `queued`, reason "stale"), at the cap it is marked failed (event `failed`).
    `started` events left dangling by the lost run on the job's worker-tier child agents are
    closed with `failed` ("aborted: ..."), so the status tree does not show them running.
    Rows are locked (SKIP LOCKED) so concurrent workers never recover the same job twice.
    Events join the caller's transaction. Returns the number of jobs recovered."""
    from ier.runtime.events import emit   # local: events imports this module's tier helpers
    retry = (config or {}).get("retry") or _cfg(None)["retry"]
    jobs = session.scalars(
        select(Job).where(Job.status == RUNNING, Job.claimed_at < _now() - older_than)
        .order_by(Job.id).with_for_update(skip_locked=True)).all()
    for job in jobs:
        claimed = job.claimed_at
        job.attempts = (job.attempts or 0) + 1
        job.claimed_at = None
        job.last_error = f"stale: claimed at {claimed.isoformat() if claimed else '?'}; worker presumed lost"
        _close_lost_workers(session, job, emit)
        if job.attempts >= int(retry["max_attempts"]):
            job.status = FAILED
            session.flush()
            emit(session, job.agent_id, "failed", {"job_id": job.id, "error": job.last_error},
                 own_transaction=False)
        else:
            job.status = QUEUED
            session.flush()
            emit(session, job.agent_id, "queued", {"job_id": job.id, "reason": "stale"},
                 own_transaction=False)
    session.flush()
    return len(jobs)


def _close_lost_workers(session: Session, job: Job, emit) -> None:
    """Close `started` events of the job's worker-tier children (they have no jobs of their
    own, so a dangling `started` there can only come from this lost run)."""
    from ier.db.models import AgentEvent
    from sqlalchemy.dialects.postgresql import distinct_on
    children = list(session.scalars(select(Agent.id).where(
        Agent.parent_id == job.agent_id, Agent.tier == tier_number("worker"))))
    if not children:
        return
    latest = session.execute(
        select(AgentEvent.agent_id, AgentEvent.kind)
        .ext(distinct_on(AgentEvent.agent_id))
        .where(AgentEvent.agent_id.in_(children),
               AgentEvent.kind.in_(("queued", "started", "completed", "failed")))
        .order_by(AgentEvent.agent_id, AgentEvent.id.desc())).all()
    for aid, kind in latest:
        if kind == "started":
            emit(session, aid, "failed", {"job_id": job.id,
                                          "error": "aborted: worker lost (stale job recovered)"},
                 own_transaction=False)
