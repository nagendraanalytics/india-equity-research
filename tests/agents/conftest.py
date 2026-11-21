import pytest

from ier.tools import exchange_filings
from tests.db.conftest import engine, session  # noqa: F401

# The fixture filings live on the placeholder host; the shipped allowlist is empty (refuses all).
FIXTURE_HOSTS = {"filings.invalid": 1}


@pytest.fixture(autouse=True)
def _fixture_filings_host_allowed(monkeypatch):
    real = exchange_filings.load_config
    monkeypatch.setattr(exchange_filings, "load_config",
                        lambda *a, **k: {**real(*a, **k), "allowed_hosts": dict(FIXTURE_HOSTS)})
