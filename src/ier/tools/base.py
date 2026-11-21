"""Provenance-tracked fetch: every external fetch is logged as a Source row (tier, date, hash).

Free/official sources only. Respects a per-host minimum request interval, retries with
exponential backoff, and caches successful responses on disk keyed by URL.

Safety (FetchRefused, never retried): only http/https; hosts that are, or resolve to,
private / loopback / link-local / reserved / multicast addresses (cloud metadata included) are
refused; redirects are followed by fetch itself (at most MAX_REDIRECTS), re-checking every hop,
never by the HTTP client; a response larger than max_response_bytes is refused; with
`allowed_hosts` (host -> source tier) every hop's host must be listed and the Source tier is
the listed tier (the caller's tier is ignored). Residual: the address check and the connection
resolve the name separately, so DNS rebinding between the two is not prevented; a host that
does not resolve at check time is not refused (the connection then fails as well).
"""
from __future__ import annotations

import hashlib
import ipaddress
import logging
import os
import socket
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx
import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from ier.db.models import Source

ROOT = Path(__file__).resolve().parents[3]
CONFIG_PATH = ROOT / "config" / "tools.yaml"
DEFAULTS = {"min_interval_seconds": 1.0, "cache_dir": "data/cache",
            "max_response_bytes": 20 * 1024 * 1024, "timeout_seconds": 30.0}
MAX_ATTEMPTS = 3
MAX_REDIRECTS = 5
_REDIRECTS = (301, 302, 303, 307, 308)
BACKOFF_BASE_SECONDS = 1.0
USER_AGENT = "ier-research/0.1 (personal research; respects robots and rate limits)"

log = logging.getLogger(__name__)

# host -> clock() reading of the last reserved request slot; process-wide so all threads
# share limits. Guarded by _rate_lock. Multi-process limiting is NOT covered.
_last_request: dict[str, float] = {}
_rate_lock = threading.Lock()


class FetchRefused(ValueError):
    """The fetch was refused by policy (host allowlist, private address, size, scheme)."""


@dataclass
class FetchResult:
    source_id: int
    content: bytes
    changed: bool


def load_config(path: Path = CONFIG_PATH) -> dict:
    cfg = dict(DEFAULTS)
    if path.exists():
        with open(path) as f:
            cfg.update(yaml.safe_load(f) or {})
    return cfg


def _resolve(host: str) -> list[str]:
    """IP addresses `host` resolves to ([] if it does not resolve). Test seam."""
    try:
        return sorted({info[4][0] for info in socket.getaddrinfo(host, None)})
    except (socket.gaierror, UnicodeError, OSError):
        return []


def _blocked_ip(text: str) -> bool:
    try:
        ip = ipaddress.ip_address(text.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return not ip.is_global or ip.is_multicast


def check_url(url: str, allowed_hosts: dict[str, int] | None = None) -> str:
    """Return the lower-cased host of `url` if it may be fetched, else raise FetchRefused.

    http/https with a host only; with `allowed_hosts`, the host must be a key; the host must
    not be, or resolve to, a non-public address (private, loopback, link-local incl. the
    169.254.169.254 metadata service, reserved, multicast)."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise FetchRefused(f"url must be http(s) with a host, got {url!r}")
    host = parts.hostname.lower()
    if allowed_hosts is not None and host not in allowed_hosts:
        raise FetchRefused(f"host {host!r} is not in allowed_hosts")
    try:
        ipaddress.ip_address(host.split("%", 1)[0])
        addresses = [host]               # a literal IP address
    except ValueError:
        addresses = _resolve(host)
    bad = [a for a in addresses if _blocked_ip(a)]
    if bad:
        raise FetchRefused(f"host {host!r} is a non-public address ({', '.join(bad)})")
    return host


def _get(client: httpx.Client, url: str, allowed_hosts, max_bytes: int, timeout: float):
    """GET with fetch-controlled redirects and a size cap. Returns (final host, body)."""
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        host = check_url(current, allowed_hosts)
        with client.stream("GET", current, follow_redirects=False, timeout=timeout) as resp:
            if resp.status_code in _REDIRECTS and resp.headers.get("location"):
                current = urljoin(current, resp.headers["location"])
                continue
            resp.raise_for_status()
            declared = resp.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > max_bytes:
                raise FetchRefused(f"response size {declared} bytes exceeds {max_bytes} for {current}")
            body = bytearray()
            for chunk in resp.iter_bytes():
                body += chunk
                if len(body) > max_bytes:
                    raise FetchRefused(f"response size exceeds {max_bytes} bytes for {current}")
            return host, bytes(body)
    raise FetchRefused(f"more than {MAX_REDIRECTS} redirects from {url}")


def _cache_path(cfg: dict, url: str) -> Path:
    d = Path(cfg["cache_dir"])
    if not d.is_absolute():
        d = ROOT / d
    return d / hashlib.sha256(url.encode()).hexdigest()


def _throttle(url: str, min_interval: float, sleep, clock) -> None:
    """Reserve the next slot for this host under a lock, then sleep outside the lock.

    Thread-safe within one process only; separate processes do not coordinate.
    """
    host = urlsplit(url).netloc.lower()
    with _rate_lock:
        now = clock()
        last = _last_request.get(host)
        slot = now if last is None else max(now, last + min_interval)
        _last_request[host] = slot
    wait = slot - now
    if wait > 0:
        sleep(wait)


def _write_cache(cache: Path, content: bytes) -> None:
    """Atomic write (temp file + os.replace); failure is logged, never raised."""
    tmp = None
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=cache.parent, prefix=".tmp-")
        with os.fdopen(fd, "wb") as f:
            f.write(content)
        os.replace(tmp, cache)
        tmp = None
    except OSError as exc:
        log.warning("cache write failed for %s: %s", cache, exc)
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _read_cache(cache: Path, expected_hash: str | None) -> bytes:
    """Cached bytes only if they match the stored content hash; otherwise b""."""
    try:
        data = cache.read_bytes()
    except OSError:
        return b""
    if expected_hash is None or hashlib.sha256(data).hexdigest() != expected_hash:
        return b""
    return data


def fetch(
    url: str,
    tier: int | None,
    session: Session,
    *,
    client: httpx.Client | None = None,
    sleep=time.sleep,
    clock=time.monotonic,
    config: dict | None = None,
    allowed_hosts: dict[str, int] | None = None,
) -> FetchResult:
    """Fetch url, log provenance, and report whether content changed since last fetch.

    After 3 failed attempts the Source is marked "stale" and the cached bytes (or b"")
    are returned with changed=False; no exception is raised. A policy refusal (see the module
    docstring) raises FetchRefused at once, before any Source row is written for a new URL.
    `allowed_hosts` (host -> tier): every hop must be listed and the Source tier is the listed
    tier of the URL's host (the least trusted hop's, if a redirect changed host); `tier` is
    then ignored. The caller owns the transaction (this function flushes, never commits).
    """
    if allowed_hosts is not None:
        allowed_hosts = {str(h).lower(): int(t) for h, t in allowed_hosts.items()}
        if not allowed_hosts:
            raise FetchRefused("allowed_hosts is empty: nothing may be fetched")
    host = check_url(url, allowed_hosts)
    if allowed_hosts is not None:
        tier = allowed_hosts[host]
    if tier not in (1, 2, 3):
        raise ValueError(f"tier must be 1-3, got {tier!r}")
    cfg = {**load_config(), **(config or {})}
    cache = _cache_path(cfg, url)
    max_bytes = int(cfg["max_response_bytes"])
    timeout = float(cfg["timeout_seconds"])

    own_client = client is None
    if own_client:
        client = httpx.Client(
            follow_redirects=False, timeout=timeout, headers={"User-Agent": USER_AGENT}
        )
    content: bytes | None = None
    try:
        for attempt in range(MAX_ATTEMPTS):
            if attempt:
                sleep(BACKOFF_BASE_SECONDS * 2 ** (attempt - 1))
            _throttle(url, float(cfg["min_interval_seconds"]), sleep, clock)
            try:
                final_host, content = _get(client, url, allowed_hosts, max_bytes, timeout)
                if allowed_hosts is not None:
                    tier = max(tier, allowed_hosts[final_host])
                break
            except (httpx.HTTPError, httpx.InvalidURL, httpx.StreamError, httpx.CookieConflict):
                continue
    finally:
        if own_client:
            client.close()

    src = session.scalars(select(Source).where(Source.url == url)).first()
    if src is None:
        src = Source(url=url, type="http", tier=tier)
        session.add(src)
    elif allowed_hosts is not None:
        src.tier = tier   # the allowlist decides how far this host is trusted

    if content is None:
        src.status = "stale"
        session.flush()
        cached = _read_cache(cache, src.content_hash)
        return FetchResult(src.id, cached, False)

    digest = hashlib.sha256(content).hexdigest()
    changed = src.content_hash != digest
    src.content_hash = digest
    src.fetch_date = datetime.now(timezone.utc)
    src.status = "ok"
    _write_cache(cache, content)
    session.flush()
    return FetchResult(src.id, content, changed)
