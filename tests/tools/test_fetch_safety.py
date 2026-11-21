"""F3: fetch safety. Filing fetches need a non-empty host allowlist (host -> source tier),
every redirect hop is re-checked, private/loopback/link-local/metadata addresses are refused,
responses are size-capped, and the Source tier comes from the host's allowlist entry."""
import json

import httpx
import pytest
from sqlalchemy import select

from ier.db.models import Source
from ier.tools import base
from ier.tools.base import FetchRefused, fetch
from ier.tools.exchange_filings import list_filings


@pytest.fixture(autouse=True)
def _reset_rate_state():
    base._last_request.clear()
    yield
    base._last_request.clear()


@pytest.fixture
def fc(tmp_path):
    return {"cache_dir": str(tmp_path), "min_interval_seconds": 0.0}


def _client(handler, **kw):
    return httpx.Client(transport=httpx.MockTransport(handler), **kw)


def test_redirect_to_metadata_address_is_refused(session, fc):
    seen = []

    def h(req):
        seen.append(str(req.url))
        if req.url.host == "allowed.example":
            return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data"})
        return httpx.Response(200, content=b"internal secret page")
    client = _client(h, follow_redirects=True)   # even a client that follows redirects itself
    with pytest.raises(FetchRefused):
        fetch("https://allowed.example/f.pdf", 1, session, client=client, sleep=lambda s: None,
              config=fc)
    assert seen == ["https://allowed.example/f.pdf"]          # the metadata URL is never hit


def test_redirect_off_the_allowlist_is_refused(session, fc):
    def h(req):
        if req.url.host == "good.example":
            return httpx.Response(301, headers={"location": "https://evil.example/x"})
        return httpx.Response(200, content=b"x")
    with pytest.raises(FetchRefused, match="evil.example"):
        fetch("https://good.example/a", None, session, client=_client(h), config=fc,
              allowed_hosts={"good.example": 1})


def test_redirect_within_the_allowlist_is_followed(session, fc):
    def h(req):
        if req.url.path == "/a":
            return httpx.Response(302, headers={"location": "/b"})
        return httpx.Response(200, content=b"filing text")
    r = fetch("https://good.example/a", None, session, client=_client(h), config=fc,
              allowed_hosts={"good.example": 1})
    assert r.content == b"filing text"


@pytest.mark.parametrize("url", ["http://127.0.0.1:8000/agents/tree", "http://localhost/x",
                                 "http://10.0.0.5/x", "http://[::1]/x",
                                 "http://169.254.169.254/latest/meta-data"])
def test_private_and_loopback_addresses_are_refused(session, fc, url):
    with pytest.raises(FetchRefused):
        fetch(url, 1, session, client=_client(lambda r: httpx.Response(200, content=b"x")),
              config=fc)


def test_host_resolving_to_a_private_address_is_refused(session, fc, monkeypatch):
    monkeypatch.setattr(base, "_resolve", lambda host: ["192.168.1.10"])
    with pytest.raises(FetchRefused, match="192.168.1.10"):
        fetch("https://innocent.example/x", 1, session,
              client=_client(lambda r: httpx.Response(200, content=b"x")), config=fc)


def test_oversize_response_is_refused(session, fc):
    big = b"x" * 2048
    with pytest.raises(FetchRefused, match="size"):
        fetch("https://good.example/big", 1, session,
              client=_client(lambda r: httpx.Response(200, content=big)),
              config={**fc, "max_response_bytes": 1024})
    assert session.scalars(select(Source).where(Source.url == "https://good.example/big")).first() is None


def test_source_tier_comes_from_the_hosts_allowlist_entry(session, fc):
    r = fetch("https://bse.example/f", 1, session,
              client=_client(lambda r: httpx.Response(200, content=b"x")), config=fc,
              allowed_hosts={"bse.example": 2})
    assert session.get(Source, r.source_id).tier == 2   # the caller's 1 is ignored


def test_empty_allowlist_refuses_filing_fetches(session, fc):
    def h(req):
        return httpx.Response(200, json=[])
    with pytest.raises(FetchRefused, match="allowed_hosts"):
        list_filings("INE000000001", session=session, client=_client(h),
                     config={"index_url": "https://idx.example/{isin}.json", "allowed_hosts": {}},
                     fetch_config=fc)


def test_index_entries_off_the_allowlist_are_dropped(session, fc):
    def h(req):
        return httpx.Response(200, json=[
            {"url": "http://127.0.0.1:8000/agents/tree", "filing_type": "results", "period": "Q1FY27"},
            {"url": "https://reddit.com/r/x", "filing_type": "results", "period": "Q2FY27"},
            {"url": "https://idx.example/f/1.pdf", "filing_type": "results", "period": "Q2FY27"}])
    refs = list_filings("INE000000001", session=session, client=_client(h),
                        config={"index_url": "https://idx.example/{isin}.json",
                                "allowed_hosts": {"idx.example": 1}},
                        fetch_config=fc)
    assert [r.url for r in refs] == ["https://idx.example/f/1.pdf"]
