"""Load config/sources.yaml: segments and sources."""
from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
CONFIG_PATH = ROOT / "config" / "sources.yaml"
KINDS = ("kite_prices", "local_files", "series_csv", "frankfurter", "fred", "rss_news", "derived")


def load_registry(path: Path = CONFIG_PATH) -> dict:
    """{"segments": {id: {...}}, "sources": [ {...} ]}; ValueError on a bad entry."""
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    segments = dict(raw.get("segments") or {})
    sources, seen = [], set()
    for s in raw.get("sources") or []:
        sid = str(s.get("id", ""))
        if not sid or sid in seen:
            raise ValueError(f"sources.yaml: missing or duplicate source id {sid!r}")
        if s.get("kind") not in KINDS:
            raise ValueError(f"sources.yaml: source {sid!r} has unknown kind {s.get('kind')!r}")
        if s.get("segment") not in segments:
            raise ValueError(f"sources.yaml: source {sid!r} names unknown segment {s.get('segment')!r}")
        seen.add(sid)
        sources.append(dict(s))
    return {"segments": segments, "sources": sources}


def get_source(reg: dict, source_id: str) -> dict | None:
    return next((s for s in reg["sources"] if s["id"] == source_id), None)
