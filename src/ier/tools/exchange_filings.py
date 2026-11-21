"""Exchange filings connector (source tier 1).

Reads a configurable filings index (JSON list per ISIN) and returns references to filing files.
UNVERIFIED: exchange access and terms of use could not be verified; see
config/exchange_filings.yaml. Every request goes through `fetch`, so it is logged as a Source.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import yaml
from sqlalchemy.orm import Session

from ier.tools.base import FetchRefused, fetch

CONFIG_PATH = Path(__file__).resolve().parent / "config" / "exchange_filings.yaml"
DEFAULTS = {"index_url": "", "allowed_hosts": {}, "local_dir": "", "local_tier": 2}


log = logging.getLogger(__name__)


@dataclass(frozen=True)
class FilingRef:
    isin: str
    url: str
    filing_type: str
    period: str
    published_date: date | None = None


def load_config(path: Path = CONFIG_PATH) -> dict:
    cfg = dict(DEFAULTS)
    if path.exists():
        with open(path) as f:
            cfg.update(yaml.safe_load(f) or {})
    return cfg


def allowed_hosts(cfg: dict) -> dict[str, int]:
    """The host -> source tier allowlist from `cfg`. FetchRefused when it is empty: filing
    fetches are refused until the owner has verified the exchange hosts and listed them.
    ValueError for a malformed list (it must be a mapping of host to tier 1-3)."""
    hosts = cfg.get("allowed_hosts") or {}
    if not isinstance(hosts, dict):
        raise ValueError("exchange_filings allowed_hosts must map host -> source tier (1-3), "
                         f"got {type(hosts).__name__}")
    out = {}
    for host, tier in hosts.items():
        if isinstance(tier, bool) or tier not in (1, 2, 3):
            raise ValueError(f"exchange_filings allowed_hosts[{host!r}]: tier must be 1-3, got {tier!r}")
        out[str(host).lower()] = int(tier)
    if not out:
        raise FetchRefused(
            "exchange_filings allowed_hosts is empty: filing fetches are refused until the "
            "exchange hosts are verified and listed (host: tier) in "
            "src/ier/tools/config/exchange_filings.yaml")
    return out


def list_filings(
    isin: str,
    *,
    session: Session,
    client: httpx.Client | None = None,
    config: dict | None = None,
    fetch_config: dict | None = None,
) -> list[FilingRef]:
    """Filings listed in the index for `isin`, newest first. Empty if none or index unavailable.

    The index and every listed filing must be on a host in allowed_hosts (host -> tier);
    entries elsewhere are dropped. FetchRefused if allowed_hosts is empty or the index URL or
    one of its redirects is refused."""
    cfg = {**load_config(), **(config or {})}
    if cfg.get("local_dir"):  # owner-downloaded files; takes priority over the web index
        from ier.tools.local_filings import list_local
        return list_local(isin, cfg["local_dir"])
    if not cfg["index_url"]:
        return []
    hosts = allowed_hosts(cfg)
    res = fetch(
        cfg["index_url"].format(isin=isin), None, session,
        client=client, config=fetch_config, allowed_hosts=hosts,
    )
    if not res.content:
        return []
    try:
        items = json.loads(res.content)
    except ValueError as exc:
        log.warning("unparseable filings index for %s: %s", isin, exc)
        return []
    if not isinstance(items, list):
        log.warning("filings index for %s is not a list", isin)
        return []
    refs = []
    for it in items:
        try:
            refs.append(_parse(isin, it, set(hosts)))
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            log.warning("dropping invalid filing entry for %s: %s", isin, exc)
    return sorted(refs, key=lambda r: (r.published_date or date.min, r.url), reverse=True)


def _valid_url(url, allowed: set[str]) -> str:
    """http(s) URL with a host in `allowed` (fetch re-checks it, its redirects and addresses)."""
    if not isinstance(url, str):
        raise ValueError(f"url must be a string, got {type(url).__name__}")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"url must be http(s) with a host, got {url!r}")
    if parts.hostname.lower() not in allowed:
        raise ValueError(f"host {parts.hostname!r} not in allowed_hosts")
    return url


def _parse(isin: str, it: dict, allowed: set[str] | None = None) -> FilingRef:
    pub = it.get("published_date")
    if not isinstance(it["filing_type"], str) or not isinstance(it["period"], str):
        raise ValueError("filing_type and period must be strings")
    return FilingRef(
        isin=isin,
        url=_valid_url(it["url"], allowed or set()),
        filing_type=it["filing_type"],
        period=it["period"],
        published_date=date.fromisoformat(pub) if pub else None,
    )
