import hashlib

import httpx
import pytest
from sqlalchemy import select

from ier.db.models import Source
from ier.tools import base
from ier.tools.base import FetchResult, fetch


class FakeTime:
    """Fake clock whose sleep advances it, so no real waiting happens."""

    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def ft():
    return FakeTime()


@pytest.fixture(autouse=True)
def _reset_rate_state():
    base._last_request.clear()
    yield
    base._last_request.clear()


@pytest.fixture
def cfg(tmp_path):
    return {"min_interval_seconds": 1.0, "cache_dir": str(tmp_path / "cache")}


def make_client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def run(url, session, ft, cfg, client, tier=1):
    return fetch(url, tier, session, client=client, sleep=ft.sleep, clock=ft.clock, config=cfg)


def test_records_source_with_hash_and_tier(session, ft, cfg):
    body = b"hello nse"
    client = make_client(lambda r: httpx.Response(200, content=body))
    res = run("https://example.com/a.csv", session, ft, cfg, client, tier=2)
    assert isinstance(res, FetchResult)
    assert res.content == body and res.changed is True
    src = session.get(Source, res.source_id)
    assert src.url == "https://example.com/a.csv"
    assert src.tier == 2
    assert src.content_hash == hashlib.sha256(body).hexdigest()
    assert src.fetch_date is not None
    assert src.status == "ok"


def test_unchanged_content_returns_changed_false(session, ft, cfg):
    client = make_client(lambda r: httpx.Response(200, content=b"same"))
    first = run("https://example.com/b", session, ft, cfg, client)
    second = run("https://example.com/b", session, ft, cfg, client)
    assert first.changed is True and second.changed is False
    assert second.source_id == first.source_id
    rows = session.scalars(select(Source).where(Source.url == "https://example.com/b")).all()
    assert len(rows) == 1


def test_new_content_after_change_returns_changed_true(session, ft, cfg):
    bodies = [b"v1", b"v2"]
    client = make_client(lambda r: httpx.Response(200, content=bodies.pop(0)))
    first = run("https://example.com/c", session, ft, cfg, client)
    second = run("https://example.com/c", session, ft, cfg, client)
    assert second.changed is True and second.content == b"v2"
    assert second.source_id == first.source_id
    assert session.get(Source, first.source_id).content_hash == hashlib.sha256(b"v2").hexdigest()


def test_retries_then_marks_stale(session, ft, cfg):
    calls = []

    def handler(r):
        calls.append(1)
        return httpx.Response(500)

    res = run("https://example.com/d", session, ft, cfg, make_client(handler))
    assert len(calls) == 3
    assert res.changed is False and res.content == b""
    assert session.get(Source, res.source_id).status == "stale"
    # exponential backoff (1s then 2s) between attempts
    assert ft.now - 1000.0 >= 3.0


def test_stale_returns_cached_content(session, ft, cfg):
    url = "https://example.com/e"
    ok = make_client(lambda r: httpx.Response(200, content=b"cached"))
    run(url, session, ft, cfg, ok)

    def boom(r):
        raise httpx.ConnectError("down")

    res = run(url, session, ft, cfg, make_client(boom))
    assert res.content == b"cached" and res.changed is False
    assert session.get(Source, res.source_id).status == "stale"


def test_success_after_stale_resets_status_ok(session, ft, cfg):
    url = "https://example.com/f"
    bad = run(url, session, ft, cfg, make_client(lambda r: httpx.Response(503)))
    assert session.get(Source, bad.source_id).status == "stale"
    good = run(url, session, ft, cfg, make_client(lambda r: httpx.Response(200, content=b"x")))
    assert good.source_id == bad.source_id and good.changed is True
    assert session.get(Source, good.source_id).status == "ok"


def test_rate_limit_respected(session, ft, cfg):
    stamps = []

    def handler(r):
        stamps.append(ft.now)
        return httpx.Response(200, content=b"x")

    client = make_client(handler)
    run("https://ratelimited.example/1", session, ft, cfg, client)
    run("https://ratelimited.example/2", session, ft, cfg, client)
    assert stamps[1] - stamps[0] >= 1.0
    # different host is not delayed
    before = ft.now
    run("https://other.example/1", session, ft, cfg, client)
    assert ft.now == before


def test_invalid_tier_rejected(session, ft, cfg):
    client = make_client(lambda r: httpx.Response(200, content=b"x"))
    for bad in (0, 4):
        with pytest.raises(ValueError):
            run("https://example.com/g", session, ft, cfg, client, tier=bad)


def test_cache_write_failure_still_returns_content(session, ft, cfg, monkeypatch):
    def fail(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(base.os, "replace", fail)
    client = make_client(lambda r: httpx.Response(200, content=b"data"))
    res = run("https://example.com/h", session, ft, cfg, client)
    assert res.content == b"data" and res.changed is True
    src = session.get(Source, res.source_id)
    assert src.content_hash == hashlib.sha256(b"data").hexdigest() and src.status == "ok"


def test_stale_ignores_corrupt_cache(session, ft, cfg):
    url = "https://example.com/i"
    run(url, session, ft, cfg, make_client(lambda r: httpx.Response(200, content=b"good")))
    base._cache_path(cfg, url).write_bytes(b"corrupt")
    res = run(url, session, ft, cfg, make_client(lambda r: httpx.Response(500)))
    assert res.content == b"" and res.changed is False


def test_stale_with_unreadable_cache_does_not_raise(session, ft, cfg):
    url = "https://example.com/j"
    run(url, session, ft, cfg, make_client(lambda r: httpx.Response(200, content=b"good")))
    p = base._cache_path(cfg, url)
    p.unlink()
    p.mkdir()  # reading a directory raises OSError
    res = run(url, session, ft, cfg, make_client(lambda r: httpx.Response(500)))
    assert res.content == b""


def test_non_http_url_rejected_without_source(session, ft, cfg):
    client = make_client(lambda r: httpx.Response(200, content=b"x"))
    for bad in ("ftp://example.com/x", "https:///nohost", "not a url"):
        with pytest.raises(ValueError):
            run(bad, session, ft, cfg, client)
    assert session.scalars(select(Source)).all() == []


def test_non_httperror_httpx_exception_is_failed_attempt(session, ft, cfg):
    calls = []

    def handler(r):
        calls.append(1)
        raise httpx.InvalidURL("bad")

    res = run("https://example.com/k", session, ft, cfg, make_client(handler))
    assert len(calls) == 3 and res.changed is False
    assert session.get(Source, res.source_id).status == "stale"


def test_throttle_thread_safe_spacing():
    import threading

    sleeps = []
    lock = threading.Lock()

    def sleep(s):
        with lock:
            sleeps.append(s)

    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()
        base._throttle("https://threads.example/x", 1.0, sleep, lambda: 500.0)

    ts = [threading.Thread(target=worker) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    # fixed clock: reserved waits must be 1,2,...,7 (first request has no wait)
    assert sorted(sleeps) == [float(i) for i in range(1, 8)]
