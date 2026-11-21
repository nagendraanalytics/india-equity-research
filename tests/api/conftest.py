import asyncio
import json
from datetime import date

import pytest
from starlette.testclient import TestClient

from ier.api.main import create_app
from tests.db.conftest import engine, session  # noqa: F401
from tests.runtime.conftest import factory  # noqa: F401

TODAY = date(2026, 11, 6)


@pytest.fixture
def make_app(factory):  # noqa: F811
    def make(**kw):
        kw.setdefault("poll_interval", 0.01)
        kw.setdefault("today", lambda: TODAY)
        return create_app(session_factory=factory, **kw)
    return make


@pytest.fixture
def app(make_app):
    return make_app()


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


class SSE:
    """Drives the ASGI app directly so an endless stream can be read incrementally and
    bounded by timeouts (httpx/TestClient buffer a response until it ends)."""

    def __init__(self, app, path="/stream", headers=None):
        self.app, self.path = app, path
        self.raw_headers = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
        self.queue: asyncio.Queue = asyncio.Queue()
        self.gone = asyncio.Event()
        self.buf = ""
        self.status = None
        self.headers = {}
        self.task = None

    async def start(self):
        path, _, qs = self.path.partition("?")
        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                 "method": "GET", "scheme": "http", "path": path, "raw_path": path.encode(),
                 "query_string": qs.encode(), "headers": self.raw_headers,
                 "server": ("test", 80), "client": ("t", 1), "root_path": ""}
        sent_body = False

        async def receive():
            nonlocal sent_body
            if not sent_body:
                sent_body = True
                return {"type": "http.request", "body": b"", "more_body": False}
            await self.gone.wait()
            return {"type": "http.disconnect"}

        async def send(msg):
            await self.queue.put(msg)

        self.task = asyncio.create_task(self.app(scope, receive, send))
        start = await asyncio.wait_for(self._next_msg(), 3)
        assert start["type"] == "http.response.start"
        self.status = start["status"]
        self.headers = {k.decode(): v.decode() for k, v in start["headers"]}
        return self

    async def _next_msg(self):
        return await self.queue.get()

    async def frames(self, n, timeout=3.0):
        """Return the next n non-empty SSE frames (strings, comments included)."""
        out = []
        deadline = asyncio.get_running_loop().time() + timeout
        while len(out) < n:
            while "\n\n" in self.buf and len(out) < n:
                frame, self.buf = self.buf.split("\n\n", 1)
                out.append(frame)
            if len(out) >= n:
                break
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise AssertionError(f"timed out; got {out!r}, buffer {self.buf!r}")
            try:
                msg = await asyncio.wait_for(self._next_msg(), remaining)
            except asyncio.TimeoutError:
                raise AssertionError(f"timed out; got {out!r}, buffer {self.buf!r}") from None
            if msg["type"] == "http.response.body":
                self.buf += msg.get("body", b"").decode()
        return out

    async def events(self, n, timeout=3.0):
        """Next n agent_event frames, parsed; keep-alive/comment frames are skipped."""
        got = []
        deadline = asyncio.get_running_loop().time() + timeout
        while len(got) < n:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise AssertionError(f"timed out waiting for {n} events, got {got!r}")
            (frame,) = await self.frames(1, remaining)
            if frame.startswith(":"):
                continue
            fields = dict(line.split(": ", 1) for line in frame.split("\n"))
            got.append({"id": int(fields["id"]), "event": fields["event"],
                        "data": json.loads(fields["data"])})
        return got

    async def expect_silence(self, seconds):
        """No agent_event frame arrives within `seconds` (comments are fine)."""
        try:
            await self.events(1, timeout=seconds)
        except (AssertionError, asyncio.TimeoutError):
            return
        raise AssertionError("unexpected event frame")

    async def disconnect(self, timeout=3.0):
        self.gone.set()
        await asyncio.wait_for(self.task, timeout)  # the app must finish by itself
