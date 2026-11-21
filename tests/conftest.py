"""Test isolation from the owner's local settings.

The committed exchange_filings.yaml carries the owner's `local_dir` (their data folder), which
switches list_filings into local-folder mode. Tests exercise the web-index path with mocked
clients, so they must not depend on that setting. Tests that want local mode pass
`config={"local_dir": ...}` explicitly; `config` overrides load_config() in list_filings.
"""
import pytest

from ier.tools import exchange_filings


@pytest.fixture(autouse=True, scope="session")
def _no_owner_local_dir():
    """Session scope so module-scoped fixtures (the golden run) are covered too."""
    monkeypatch = pytest.MonkeyPatch()
    real = exchange_filings.load_config

    def load_config(*args, **kwargs):
        return {**real(*args, **kwargs), "local_dir": ""}

    monkeypatch.setattr(exchange_filings, "load_config", load_config)
    # graph.company imported the function by name
    from ier.graph import company
    monkeypatch.setattr(company, "load_filings_config", load_config)
    yield
    monkeypatch.undo()
