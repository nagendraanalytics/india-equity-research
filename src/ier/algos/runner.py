"""Run every enabled algorithm over one company's bars. Never raises for data problems."""
from __future__ import annotations

import importlib
import logging
from datetime import date

from ier.algos.core import AlgoResult, Bar, Ctx
from ier.algos.registry import all_algorithms

log = logging.getLogger(__name__)
_FAMILY_MODULES = ("trend", "momentum", "risk", "breakout", "volume", "relative", "fundamental")


def load_all() -> None:
    for m in _FAMILY_MODULES:
        importlib.import_module(f"ier.algos.{m}")


def run_algorithms(bars: list[Bar], isin: str, cfg: dict, *, index_bars: list[Bar] | None = None,
                   peer_momentum: dict[str, float] | None = None, today: date) -> dict[str, AlgoResult]:
    load_all()
    enabled = cfg.get("enabled_families")
    algos = [a for a in all_algorithms() if not enabled or a.family in enabled]
    if not bars:
        return {a.id: AlgoResult(None, None, "needs daily prices") for a in algos}
    last = bars[-1].day
    if (today - last).days > int(cfg.get("max_stale_days", 10)):
        return {a.id: AlgoResult(None, None, f"prices stale (last {last.isoformat()})") for a in algos}
    ctx = Ctx(bars=bars, index_bars=index_bars, peer_momentum=peer_momentum, isin=isin, cfg=cfg)
    out: dict[str, AlgoResult] = {}
    for a in algos:
        try:
            out[a.id] = a.compute(ctx)
        except Exception:  # noqa: BLE001 - one bad algorithm must not stop the rest
            log.exception("algorithm %s failed for %s", a.id, isin)
            out[a.id] = AlgoResult(None, None, "calculation failed")
    return out
