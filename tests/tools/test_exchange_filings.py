import json
from datetime import date

import httpx

from ier.tools.exchange_filings import FilingRef, list_filings

CFG = {"index_url": "https://filings.invalid/index/{isin}.json",
       "allowed_hosts": {"filings.invalid": 1}}
FETCH_CFG = {"min_interval_seconds": 0.0}


def _client(body, status=200):
    return httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(status, content=body)))


def test_lists_filings_newest_first(session, tmp_path):
    items = [
        {"url": "https://filings.invalid/a", "filing_type": "results", "period": "Q1FY27", "published_date": "2026-07-15"},
        {"url": "https://filings.invalid/b", "filing_type": "results", "period": "Q2FY27", "published_date": "2026-10-16"},
    ]
    refs = list_filings("INE1", session=session, client=_client(json.dumps(items).encode()),
                        config=CFG, fetch_config={**FETCH_CFG, "cache_dir": str(tmp_path)})
    assert [r.period for r in refs] == ["Q2FY27", "Q1FY27"]
    assert refs[0] == FilingRef("INE1", "https://filings.invalid/b", "results", "Q2FY27", date(2026, 10, 16))


def test_bad_or_failed_index_gives_empty(session, tmp_path):
    fc = {**FETCH_CFG, "cache_dir": str(tmp_path), "min_interval_seconds": 0.0}
    assert list_filings("INE2", session=session, client=_client(b"not json"), config=CFG, fetch_config=fc) == []


def test_allowlist_and_scheme_validation(session, tmp_path):
    items = [
        {"url": "ftp://good.example/a", "filing_type": "results", "period": "Q1"},
        {"url": "https://evil.example/b", "filing_type": "results", "period": "Q1"},
        {"url": 5, "filing_type": "results", "period": "Q1"},
        {"url": "https://good.example/c", "filing_type": "results", "period": "Q1"},
    ]
    refs = list_filings("INE3", session=session, client=_client(json.dumps(items).encode()),
                        config={**CFG, "allowed_hosts": {"filings.invalid": 1, "good.example": 1}},
                        fetch_config={**FETCH_CFG, "cache_dir": str(tmp_path)})
    assert [r.url for r in refs] == ["https://good.example/c"]
