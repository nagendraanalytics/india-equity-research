"""Universe filter: drop suspended, surveillance-listed, illiquid and very new listings."""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from ier.db.models import Instrument

CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "universe.yaml"


def load_config(path: Path = CONFIG_PATH) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def build_universe(
    session: Session,
    macro_sector: str | None,
    min_listing_days: int = 365,
    today: date | None = None,
) -> list[Instrument]:
    """Eligible instruments ordered by name.

    Instruments with unknown listing_date or avg_daily_value are excluded because
    eligibility cannot be verified. macro_sector=None applies no sector filter.
    """
    cfg = load_config()
    cutoff = (today or date.today()) - timedelta(days=min_listing_days)
    stmt = select(Instrument).where(
        Instrument.status.not_in(cfg["excluded_statuses"]),
        Instrument.surveillance_flag.is_(None),
        Instrument.listing_date.is_not(None),
        Instrument.listing_date <= cutoff,
        Instrument.avg_daily_value.is_not(None),
        Instrument.avg_daily_value >= cfg["min_avg_daily_value"],
    )
    if macro_sector is not None:
        stmt = stmt.where(Instrument.macro_sector == macro_sector)
    return list(session.scalars(stmt.order_by(Instrument.name, Instrument.id)))
