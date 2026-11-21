"""Parallel workers: one thread pool per tier, each thread with its own Session (spec 4.2a).

Per job: AgentEvent `started`, run the handler (keyed by Agent.role), commit on success with
`completed`; on exception roll back, fail() (bounded retries) and write `failed`.
BudgetExceeded, by cap scope:
  * per_run (this job's own budget; the per-run scope is the job's agent, see
    ier.llm.router): roll back and mark THIS job failed (no retry: it would hit the same cap);
    other workers go on.
  * per_day: roll back, put the job back in the queue for the next UTC midnight (attempts
    unchanged) and set the shared halt event so every worker in every tier stops claiming;
    run_workers then returns.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.orm import Session

from ier.db import session as db_session
from ier.db.models import Agent, AgentEvent, Job
from ier.llm.router import CAP_SCOPE_KEY, BudgetExceeded
from ier.runtime.events import emit
from ier.runtime.queue import (claim_next, complete, fail, fail_now, release, requeue_stale,
                               stale_settings, tier_number)

log = logging.getLogger(__name__)

Handler = Callable[[Session, Job], None]
# Default registry, keyed by Agent.role; later tasks register real agent runners here.
HANDLERS: dict[str, Handler] = {}

_WAIT_SLICE = 0.05  # max latency for noticing stop/halt while idle


def _event(session: Session, agent_id: int, kind: str, job_id: int, *, join: bool = False,
           **extra) -> None:
    """`join=True` writes the event in the job transaction (terminal events: they commit or
    roll back together with the job status, so the log never says completed for a job that is
    not done). `started` is independent so the console shows it while the job runs."""
    emit(session, agent_id, kind, {"job_id": job_id, **extra}, own_transaction=not join)


_STATUS = ("queued", "started", "completed", "failed")


def _close_dangling(session: Session, started: list[tuple[int, int]], job_id: int,
                    why: str) -> None:
    """After a rollback: close the `started` events THIS job emitted (`started` holds
    (agent_id, event_id) pairs recorded by emit() while the handler ran). Those independently
    committed events outlived the rolled-back work, so each agent whose latest status event is
    still that same event gets a `failed` event (joined to the job transaction). An agent that
    has since been started by another job (e.g. a sibling company job under the same sector)
    has a newer status event and is left alone.

    Note: closing aborted workers as `failed` makes the tree roll the company agent and its
    ancestors up as 'degraded' after a budget halt, until the next successful run. This is
    intended: an aborted run is shown, not hidden."""
    if not started:
        return
    agent_ids = {aid for aid, _ in started}
    latest = dict(session.execute(
        select(AgentEvent.agent_id, AgentEvent.id)
        .ext(distinct_on(AgentEvent.agent_id))
        .where(AgentEvent.agent_id.in_(agent_ids), AgentEvent.kind.in_(_STATUS))
        .order_by(AgentEvent.agent_id, AgentEvent.id.desc())).all())
    for aid, eid in started:
        if latest.get(aid) == eid:
            _event(session, aid, "failed", job_id, join=True, error=f"aborted: {why}")


def _wait(stop: threading.Event, halt: threading.Event, seconds: float) -> None:
    remaining = seconds
    while remaining > 0 and not (stop.is_set() or halt.is_set()):
        step = min(_WAIT_SLICE, remaining)
        stop.wait(step)
        remaining -= step


def _run_job(session, job, handlers, halt, config) -> None:
    job_id, agent_id = job.id, job.agent_id
    role = session.get(Agent, agent_id).role
    _event(session, agent_id, "started", job_id)
    session.commit()
    handler = handlers.get(role)
    if handler is None:
        error = f"no handler registered for role {role!r}"
        fail(session, job, error, config=config)
        _event(session, agent_id, "failed", job_id, join=True, error=error)
        session.commit()
        return
    started: list[tuple[int, int]] = []
    session.info["started_events"] = started
    session.info[CAP_SCOPE_KEY] = agent_id     # per-run compute cap = this job
    try:
        handler(session, job)
        complete(session, job)
        _event(session, agent_id, "completed", job_id, join=True)
        session.commit()
    except BudgetExceeded as exc:
        session.rollback()
        if getattr(exc, "scope", "per_day") == "per_run":
            error = f"BudgetExceeded: {exc}"
            log.warning("per-run compute cap reached in job %s; job failed: %s", job_id, exc)
            _close_dangling(session, started, job_id, "per-run compute cap reached")
            fail_now(session, session.get(Job, job_id), error)
            _event(session, agent_id, "failed", job_id, join=True, error=error[:500])
            session.commit()
        else:
            log.warning("compute cap reached in job %s; halting all workers: %s", job_id, exc)
            _close_dangling(session, started, job_id, "compute cap reached")
            release(session, session.get(Job, job_id), until_next_midnight=True)
            session.commit()
            halt.set()
    except Exception as exc:  # noqa: BLE001 - failures are isolated to the job
        session.rollback()
        error = f"{type(exc).__name__}: {exc}"
        log.warning("job %s failed: %s", job_id, error)
        _close_dangling(session, started, job_id, "job failed")
        fail(session, session.get(Job, job_id), error, config=config)
        _event(session, agent_id, "failed", job_id, join=True, error=error[:500])
        session.commit()
    finally:
        session.info.pop("started_events", None)
        session.info.pop(CAP_SCOPE_KEY, None)


def _recover_stale(session: Session, config: dict | None) -> None:
    """requeue_stale with the configured age; errors are logged (never kill a worker)."""
    older_than, _ = stale_settings(config)
    try:
        n = requeue_stale(session, older_than, config=config)
        session.commit()
        if n:
            log.warning("recovered %d stale running job(s) (claimed over %s ago)", n, older_than)
    except Exception:  # noqa: BLE001 - e.g. a dropped connection; next interval retries
        log.exception("requeue_stale failed")
        session.rollback()


def _worker(tier, stop, halt, factory, handlers, poll_interval, config) -> None:
    session = factory()
    _, interval = stale_settings(config)
    next_check = time.monotonic() + interval
    try:
        while not (stop.is_set() or halt.is_set()):
            if time.monotonic() >= next_check:
                _recover_stale(session, config)
                next_check = time.monotonic() + interval
            try:
                job = claim_next(session, tier)
                if job is not None and halt.is_set():   # halted while we were claiming
                    session.rollback()
                    break
                session.commit()
            except Exception:  # noqa: BLE001 - e.g. a dropped connection; retry after a pause
                log.exception("claim failed for tier %s", tier)
                session.rollback()
                _wait(stop, halt, poll_interval)
                continue
            if job is None:
                _wait(stop, halt, poll_interval)
                continue
            job_id = job.id  # captured now: after a failed rollback job.id may need the DB
            try:
                _run_job(session, job, handlers, halt, config)
            except Exception:  # noqa: BLE001 - bookkeeping failed; requeue_stale recovers
                try:
                    session.rollback()
                except Exception:  # noqa: BLE001 - connection is gone; drop it, keep looping
                    session.close()
                log.exception("bookkeeping failed for job %s; left for requeue_stale", job_id)
    finally:
        session.close()


def run_workers(
    tiers: dict[str, int],
    stop: threading.Event,
    *,
    session_factory: Callable[[], Session] | None = None,
    handlers: dict[str, Handler] | None = None,
    poll_interval: float = 0.5,
    halt: threading.Event | None = None,
    config: dict | None = None,
) -> None:
    """Run `tiers[name]` worker threads per tier until `stop` is set or a compute cap halts
    all workers. Blocks; returns once every worker thread has exited.

    `stop` and `halt` are checked between jobs and while idle (within ~50 ms); they cannot
    interrupt a handler that is already running, so shutdown waits for in-flight handlers.
    Worker threads are daemon threads: if the process exits while run_workers is still
    blocked, in-flight jobs are abandoned as `running`. requeue_stale recovers them: once at
    start and then every `stale.check_interval_seconds` in each worker loop, for jobs claimed
    more than `stale.after_seconds` ago (config/runtime.yaml).
    """
    for name in tiers:
        tier_number(name)  # ValueError before any thread starts
    if session_factory is None:
        db_session.get_engine()  # initialise the shared engine once, before threads race
        session_factory = db_session.get_session
    if handlers is None:
        # default registry: make sure the company/sector/industry runners are in it (local
        # import: ier.graph.company imports this module)
        from ier.graph.company import register_default_handlers
        handlers = register_default_handlers(HANDLERS, overwrite=False)
    halt = halt or threading.Event()
    with session_factory() as s:      # jobs left running by a crashed earlier process
        _recover_stale(s, config)
    threads = [
        threading.Thread(
            target=_worker, name=f"ier-worker-{tier}-{i}", daemon=True,
            args=(tier, stop, halt, session_factory, handlers, poll_interval, config),
        )
        for tier, n in tiers.items() for i in range(n)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
