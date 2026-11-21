import threading
from datetime import datetime, timedelta, timezone

import pytest

from ier.db.models import Agent, Document, Instrument, Job, Source
from ier.runtime.queue import (
    claim_next,
    complete,
    enqueue,
    fail,
    release,
    requeue_stale,
    tier_number,
)
from ier.runtime.triggers import on_manual, on_market_event, on_new_source, on_schedule

NO_BACKOFF = {"retry": {"max_attempts": 3, "backoff_base_seconds": 0}}


def _agent(session, tier="company", role="company", **kw):
    a = Agent(tier=tier_number(tier), role=role, **kw)
    session.add(a)
    session.flush()
    return a


def _inst(session, isin, sector="Information Technology"):
    i = Instrument(isin=isin, name=isin, sector=sector)
    session.add(i)
    session.flush()
    return i


def _source(session, content_hash="h1", instrument=None, sector=None):
    s = Source(url=f"https://x.invalid/{content_hash}/{instrument and instrument.id}/{sector}",
               type="filing", tier=1, content_hash=content_hash)
    session.add(s)
    session.flush()
    session.add(Document(source_id=s.id, instrument_id=instrument.id if instrument else None,
                         sector=sector))
    session.flush()
    return s


# --- tiers ----------------------------------------------------------------------------

def test_tier_names_map_in_spec_order():
    names = ["master", "macro_sector", "sector", "industry", "company", "worker"]
    assert [tier_number(n) for n in names] == [1, 2, 3, 4, 5, 6]


def test_unknown_tier_raises_value_error(session):
    with pytest.raises(ValueError):
        claim_next(session, "galaxy")


# --- claiming -------------------------------------------------------------------------

def test_job_claimed_by_one_worker_only(factory):
    with factory() as s:
        a = _agent(s)
        enqueue(s, a.id, "manual")
        s.commit()

    barrier = threading.Barrier(2, timeout=5)
    results = []
    lock = threading.Lock()

    def claimant():
        with factory() as s:
            barrier.wait()          # both start claiming together
            job = claim_next(s, "company")
            barrier.wait()          # both claims overlap inside open transactions
            s.commit()
            with lock:
                results.append(job.id if job else None)

    threads = [threading.Thread(target=claimant) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert not any(t.is_alive() for t in threads)
    assert sorted(results, key=lambda r: r is None) == [1, None]
    with factory() as s:
        job = s.get(Job, 1)
        assert job.status == "running" and job.claimed_at is not None


def test_concurrent_claimants_never_share_a_job(factory):
    with factory() as s:
        for _ in range(12):   # one agent per job: an agent never runs two jobs at once (F7)
            enqueue(s, _agent(s).id, "manual")
        s.commit()
    claimed, lock = [], threading.Lock()
    barrier = threading.Barrier(4, timeout=5)

    def claimant():
        with factory() as s:
            barrier.wait()
            while (job := claim_next(s, "company")) is not None:
                s.commit()
                with lock:
                    claimed.append(job.id)

    threads = [threading.Thread(target=claimant) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert sorted(claimed) == list(range(1, 13))


def test_claim_only_matches_requested_tier(session):
    sector = _agent(session, tier="sector", role="sector")
    enqueue(session, sector.id, "manual")
    assert claim_next(session, "company") is None
    assert claim_next(session, "sector") is not None


def test_future_scheduled_job_not_claimed(session):
    a = _agent(session)
    later = enqueue(session, a.id, "schedule", datetime.now(timezone.utc) + timedelta(hours=1))
    assert claim_next(session, "company") is None
    later.scheduled_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    session.flush()
    assert claim_next(session, "company").id == later.id


def test_claim_order_by_due_time_then_id(session):
    a, b, c, d = (_agent(session) for _ in range(4))     # distinct agents (F7: one running job each)
    raw = Job(agent_id=a.id, trigger="manual")           # NULL scheduled_at sorts first
    session.add(raw)
    session.flush()
    first = enqueue(session, b.id, "manual")              # due at enqueue time
    past = enqueue(session, c.id, "schedule", datetime.now(timezone.utc) - timedelta(minutes=5))
    second = enqueue(session, d.id, "manual")
    assert first.scheduled_at is not None
    got = [claim_next(session, "company").id for _ in range(4)]
    assert got == [raw.id, past.id, first.id, second.id]


def test_backed_off_retry_not_starved_by_newer_jobs(session):
    a = _agent(session)
    retry = enqueue(session, a.id, "manual")
    claim_next(session, "company")
    fail(session, retry, "flaky", config=NO_BACKOFF)       # due again now
    newer = [enqueue(session, _agent(session).id, "manual") for _ in range(3)]
    assert claim_next(session, "company").id == retry.id
    assert [claim_next(session, "company").id for _ in newer] == [j.id for j in newer]


def test_enqueue_rejects_naive_datetime(session):
    a = _agent(session)
    with pytest.raises(ValueError):
        enqueue(session, a.id, "schedule", datetime(2026, 11, 6, 9, 0))
    with pytest.raises(ValueError):
        on_schedule(session, [a.id], datetime(2026, 11, 6, 9, 0))


def test_depends_on_respected(session):
    a = _agent(session)
    parent = enqueue(session, a.id, "manual")
    child = enqueue(session, a.id, "manual", depends_on=parent.id)
    assert claim_next(session, "company").id == parent.id
    assert claim_next(session, "company") is None       # parent running, not done
    fail(session, parent, "boom", config={"retry": {"max_attempts": 1, "backoff_base_seconds": 0}})
    assert parent.status == "failed"
    # final review F5: a failed parent is settled; the dependent runs without its output
    assert claim_next(session, "company").id == child.id


def test_queued_parent_still_blocks(session):
    a = _agent(session)
    parent = enqueue(session, a.id, "manual", scheduled_at=datetime.now(timezone.utc) + timedelta(hours=1))
    enqueue(session, a.id, "manual", depends_on=parent.id)
    assert claim_next(session, "company") is None       # parent in backoff: child waits


def test_requeue_stale(session):
    old = enqueue(session, _agent(session).id, "manual")
    fresh = enqueue(session, _agent(session).id, "manual")
    claim_next(session, "company")
    claim_next(session, "company")
    old.claimed_at = datetime.now(timezone.utc) - timedelta(hours=2)
    session.flush()
    assert requeue_stale(session, timedelta(hours=1)) == 1
    session.refresh(old)
    session.refresh(fresh)
    assert old.status == "queued" and old.claimed_at is None
    assert fresh.status == "running"


# --- lifecycle ------------------------------------------------------------------------

def test_complete_and_release(session):
    a = _agent(session)
    j1 = enqueue(session, a.id, "manual")
    j2 = enqueue(session, a.id, "manual")
    claim_next(session, "company")
    claim_next(session, "company")
    complete(session, j1)
    release(session, j2)
    assert j1.status == "done"
    assert j2.status == "queued" and j2.attempts == 0 and j2.claimed_at is None


def test_fail_backs_off_exponentially_then_marks_failed(session):
    a = _agent(session)
    job = enqueue(session, a.id, "manual")
    cfg = {"retry": {"max_attempts": 3, "backoff_base_seconds": 10}}
    delays = []
    for _ in range(2):
        job.status = "running"
        before = datetime.now(timezone.utc)
        fail(session, job, ValueError("bad"), config=cfg)
        assert job.status == "queued" and job.claimed_at is None
        delays.append((job.scheduled_at - before).total_seconds())
    assert 10 <= delays[0] < 11 and 20 <= delays[1] < 21
    job.status = "running"
    fail(session, job, ValueError("bad"), config=cfg)
    assert job.status == "failed" and job.attempts == 3 and "bad" in job.last_error


# --- triggers -------------------------------------------------------------------------

def test_new_filing_creates_job_for_that_company_agent_only(session):
    infy, tcs = _inst(session, "INE009A01021"), _inst(session, "INE467B01029")
    infy_agent = _agent(session, scope_instrument_id=infy.id)
    _agent(session, scope_instrument_id=tcs.id)
    _agent(session, tier="sector", role="sector", scope_name="Banks")
    src = _source(session, "h1", instrument=infy)
    jobs = on_new_source(session, src.id)
    assert [j.agent_id for j in jobs] == [infy_agent.id]
    assert jobs[0].trigger == "new_source" and jobs[0].status == "queued"
    assert jobs[0].payload == {"source_id": src.id, "content_hash": "h1"}


def test_sector_document_creates_job_for_sector_agent(session):
    it = _agent(session, tier="sector", role="sector", scope_name="Information Technology")
    _agent(session, tier="sector", role="sector", scope_name="Banks")
    src = _source(session, "h1", sector="Information Technology")
    assert [j.agent_id for j in on_new_source(session, src.id)] == [it.id]


def test_unchanged_source_creates_no_job(session):
    infy = _inst(session, "INE009A01021")
    agent = _agent(session, scope_instrument_id=infy.id)
    src = _source(session, "h1", instrument=infy)
    [job] = on_new_source(session, src.id)
    job.status = "done"
    session.flush()
    assert on_new_source(session, src.id) == []          # same hash, already processed
    src.content_hash = "h2"
    session.flush()
    [again] = on_new_source(session, src.id)             # content changed -> new job
    assert again.agent_id == agent.id and again.payload["content_hash"] == "h2"
    src.content_hash = None
    session.flush()
    again.status = "done"
    session.flush()
    assert on_new_source(session, src.id) == []          # no hash -> nothing to compare


def test_failed_prior_job_does_not_block_new_source_job(session):
    infy = _inst(session, "INE009A01021")
    _agent(session, scope_instrument_id=infy.id)
    src = _source(session, "h1", instrument=infy)
    [job] = on_new_source(session, src.id)
    job.status = "failed"
    session.flush()
    assert len(on_new_source(session, src.id)) == 1


def test_new_source_trigger_idempotent_while_active(session):
    infy = _inst(session, "INE009A01021")
    _agent(session, scope_instrument_id=infy.id)
    src = _source(session, "h1", instrument=infy)
    assert len(on_new_source(session, src.id)) == 1
    assert on_new_source(session, src.id) == []
    src.content_hash = "h2"                               # still queued: the job reads latest
    session.flush()
    assert on_new_source(session, src.id) == []


def test_running_job_with_old_hash_does_not_absorb_new_content(session):
    infy = _inst(session, "INE009A01021")
    agent = _agent(session, scope_instrument_id=infy.id)
    src = _source(session, "h1", instrument=infy)
    [running] = on_new_source(session, src.id)
    assert claim_next(session, "company").id == running.id
    src.content_hash = "h2"
    session.flush()
    [new] = on_new_source(session, src.id)
    assert new.id != running.id and new.agent_id == agent.id and new.status == "queued"
    assert new.payload == {"source_id": src.id, "content_hash": "h2"}
    assert running.payload["content_hash"] == "h1"


def test_queued_job_absorbs_new_hash_and_updates_payload(session):
    infy = _inst(session, "INE009A01021")
    _agent(session, scope_instrument_id=infy.id)
    src = _source(session, "h1", instrument=infy)
    [queued] = on_new_source(session, src.id)
    src.content_hash = "h2"
    session.flush()
    assert on_new_source(session, src.id) == []
    session.expire_all()
    assert session.query(Job).count() == 1
    assert session.get(Job, queued.id).payload == {"source_id": src.id, "content_hash": "h2"}


def test_running_job_with_same_hash_blocks_new_job(session):
    infy = _inst(session, "INE009A01021")
    _agent(session, scope_instrument_id=infy.id)
    src = _source(session, "h1", instrument=infy)
    on_new_source(session, src.id)
    claim_next(session, "company")
    assert on_new_source(session, src.id) == []


def test_unchanged_rule_is_per_source(session):
    infy = _inst(session, "INE009A01021")
    _agent(session, scope_instrument_id=infy.id)
    a = _source(session, "same", instrument=infy)
    [ja] = on_new_source(session, a.id)
    ja.status = "done"
    session.flush()
    b = Source(url="https://x.invalid/other", type="filing", tier=1, content_hash="same")
    session.add(b)
    session.flush()
    session.add(Document(source_id=b.id, instrument_id=infy.id))
    session.flush()
    assert len(on_new_source(session, b.id)) == 1   # same hash, different source -> new job


def test_running_job_does_not_absorb_manual_schedule_or_market(session):
    infy = _inst(session, "INE009A01021")
    a = _agent(session, scope_instrument_id=infy.id)
    [m] = on_manual(session, a.id)
    claim_next(session, "company")
    assert m.status == "running"
    assert len(on_manual(session, a.id)) == 1               # re-run request after start
    session.query(Job).delete()
    [e] = on_market_event(session, infy.id, "price", 0.08, 0.05)
    claim_next(session, "company")
    [e2] = on_market_event(session, infy.id, "price", 0.12, 0.05)
    assert e2.payload["magnitude"] == 0.12
    session.query(Job).delete()
    now = datetime.now(timezone.utc)
    [s1] = on_schedule(session, [a.id], now - timedelta(seconds=1))
    claim_next(session, "company")
    assert len(on_schedule(session, [a.id], now + timedelta(days=1))) == 1


def test_queued_market_job_absorbs_and_takes_latest_magnitude(session):
    infy = _inst(session, "INE009A01021")
    _agent(session, scope_instrument_id=infy.id)
    [e] = on_market_event(session, infy.id, "price", 0.08, 0.05)
    assert on_market_event(session, infy.id, "price", -0.15, 0.05) == []
    session.expire_all()
    assert session.get(Job, e.id).payload["magnitude"] == -0.15


def test_market_event_threshold_and_idempotence(session):
    infy = _inst(session, "INE009A01021")
    agent = _agent(session, scope_instrument_id=infy.id)
    assert on_market_event(session, infy.id, "price", 0.02, 0.05) == []
    [job] = on_market_event(session, infy.id, "price", -0.08, 0.05)
    assert job.agent_id == agent.id and job.trigger == "market_event"
    assert job.payload["kind"] == "price"
    assert on_market_event(session, infy.id, "price", 0.09, 0.05) == []
    assert len(on_market_event(session, infy.id, "volume", 3.0, 2.0)) == 1


def test_schedule_and_manual_triggers_idempotent(session):
    a, b = _agent(session), _agent(session)
    when = datetime.now(timezone.utc) + timedelta(hours=1)
    jobs = on_schedule(session, [a.id, b.id], when)
    assert sorted(j.agent_id for j in jobs) == [a.id, b.id]
    assert all(j.scheduled_at == when and j.trigger == "schedule" for j in jobs)
    assert on_schedule(session, [a.id, b.id], when) == []
    [m] = on_manual(session, a.id)
    assert m.trigger == "manual"
    assert on_manual(session, a.id) == []
    m.status = "done"
    session.flush()
    assert len(on_manual(session, a.id)) == 1


# --- absorption race: a queued job claimed between the trigger's lookup and its write ----

def _race(factory, monkeypatch, fire, setup):
    """Run `fire(session)` in a thread paused right after triggers._queued returns a job;
    meanwhile another session claims that job and commits. Returns (fire result ids, claimed id)."""
    import ier.runtime.triggers as trg

    looked_up, proceed = threading.Event(), threading.Event()
    real = trg._queued

    def paused(*a, **kw):
        job = real(*a, **kw)
        if job is not None and not looked_up.is_set():
            looked_up.set()
            assert proceed.wait(5)
        return job

    monkeypatch.setattr(trg, "_queued", paused)
    ctx = setup()
    out = {}

    def run():
        with factory() as s:
            out["ids"] = [j.id for j in fire(s, ctx)]
            s.commit()

    t = threading.Thread(target=run)
    t.start()
    try:
        assert looked_up.wait(5)
        with factory() as b:
            claimed = claim_next(b, "company")
            b.commit()
    finally:
        proceed.set()
        t.join(5)
    assert not t.is_alive() and "ids" in out
    return out["ids"], claimed.id, ctx


def test_new_source_absorb_race_creates_new_job_when_claimed_meanwhile(factory, monkeypatch):
    def setup():
        with factory() as s:
            infy = _inst(s, "INE009A01021")
            _agent(s, scope_instrument_id=infy.id)
            src = _source(s, "h1", instrument=infy)
            [job] = on_new_source(s, src.id)
            src.content_hash = "h2"
            s.commit()
            return src.id

    ids, claimed, src_id = _race(factory, monkeypatch, on_new_source, setup)
    with factory() as s:
        jobs = {j.id: j for j in s.query(Job)}
    assert jobs[claimed].status == "running"
    assert jobs[claimed].payload == {"source_id": src_id, "content_hash": "h1"}
    [new_id] = ids
    assert jobs[new_id].status == "queued"
    assert jobs[new_id].payload == {"source_id": src_id, "content_hash": "h2"}


def test_market_manual_schedule_absorb_race_create_new_jobs(factory, monkeypatch):
    def market_setup():
        with factory() as s:
            infy = _inst(s, "INE009A01021")
            _agent(s, scope_instrument_id=infy.id)
            on_market_event(s, infy.id, "price", 0.08, 0.05)
            s.commit()
            return infy.id

    ids, claimed, iid = _race(
        factory, monkeypatch,
        lambda s, iid: on_market_event(s, iid, "price", -0.2, 0.05), market_setup)
    with factory() as s:
        assert s.get(Job, claimed).payload["magnitude"] == 0.08
        [new_id] = ids
        assert s.get(Job, new_id).payload["magnitude"] == -0.2
        s.query(Job).delete()
        s.commit()
    monkeypatch.undo()

    for name, fire in [
        ("manual", lambda s, aid: on_manual(s, aid)),
        ("schedule", lambda s, aid: on_schedule(
            s, [aid], datetime.now(timezone.utc) - timedelta(seconds=1))),
    ]:
        def setup():
            with factory() as s:
                aid = s.query(Agent.id).scalar()
                fire(s, aid)
                s.commit()
                return aid

        ids, claimed, _ = _race(factory, monkeypatch, fire, setup)
        assert len(ids) == 1 and ids[0] != claimed, name
        with factory() as s:
            assert s.get(Job, ids[0]).status == "queued", name
            s.query(Job).delete()
            s.commit()
        monkeypatch.undo()
