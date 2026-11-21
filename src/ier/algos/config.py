"""Loader for config/algos.yaml. Hard cap on the blend weight lives here, not in the file."""
from __future__ import annotations

import hashlib
from pathlib import Path

import yaml

MAX_BLEND_WEIGHT = 0.30
DEFAULT_PATH = Path(__file__).resolve().parents[3] / "config" / "algos.yaml"


def load_algo_config(path: Path | None = None) -> dict:
    p = Path(path) if path else DEFAULT_PATH
    raw = yaml.safe_load(p.read_text()) or {}
    cfg = dict(raw)
    cfg["notes"] = []
    w = raw.get("blend_weight", 0.0)
    if isinstance(w, bool) or not isinstance(w, (int, float)):
        raise ValueError(f"blend_weight must be a number, got {w!r}")
    if w < 0:
        raise ValueError("blend_weight must not be negative")
    if w > MAX_BLEND_WEIGHT:
        cfg["notes"].append(f"blend_weight {w} above cap; clamped to {MAX_BLEND_WEIGHT}")
        w = MAX_BLEND_WEIGHT
    cfg["blend_weight"] = float(w)
    cfg["max_blend_weight"] = MAX_BLEND_WEIGHT
    cfg.setdefault("max_stale_days", 10)
    cfg.setdefault("enabled_families", None)
    cfg.setdefault("sector_index", {})
    cfg.setdefault("algos", {})
    return cfg


def config_sha(path: Path | None = None) -> str:
    p = Path(path) if path else DEFAULT_PATH
    return hashlib.sha256(p.read_bytes()).hexdigest()
