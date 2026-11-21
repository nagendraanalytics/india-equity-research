"""Load the NSE/BSE four-level industry classification into ``instruments``.

CSV columns: isin,nse_code,bse_code,name,exchange,macro_sector,sector,industry,
basic_industry. A dual-listed company has one row per exchange sharing an ISIN
and is merged into a single Instrument. Empty cells mean missing.
"""

import csv
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ier.db.models import Instrument

COLUMNS = (
    "isin", "nse_code", "bse_code", "name", "exchange",
    "macro_sector", "sector", "industry", "basic_industry",
)
LEVELS = ("macro_sector", "sector", "industry", "basic_industry")


def _clean(value: str | None) -> str | None:
    value = (value or "").strip()
    return value or None


def load_classification(path: Path, session: Session) -> int:
    """Upsert instruments by ISIN from ``path``; return the distinct ISINs written.

    Re-loading the same file changes nothing. Empty cells never overwrite
    existing values. The caller owns the transaction (the session is flushed,
    not committed).
    """
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        missing = set(COLUMNS) - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"classification CSV missing columns: {sorted(missing)}")
        rows = list(reader)

    seen: dict[str, Instrument] = {}
    set_this_load: dict[str, dict[str, str]] = {}
    for lineno, raw in enumerate(rows, start=2):
        row = {k: _clean(raw.get(k)) for k in COLUMNS}
        isin = row["isin"]
        if not isin:
            raise ValueError(f"line {lineno}: isin is required")
        exchange = (row["exchange"] or "").upper()
        if exchange not in ("NSE", "BSE"):
            raise ValueError(f"line {lineno}: exchange must be NSE or BSE, got {row['exchange']!r}")
        if not row["name"]:
            raise ValueError(f"line {lineno}: name is required")

        inst = seen.get(isin)
        if inst is None:
            inst = session.scalars(select(Instrument).where(Instrument.isin == isin)).one_or_none()
            if inst is None:
                inst = Instrument(isin=isin, name=row["name"])
                session.add(inst)
            seen[isin] = inst

        values = {"name": row["name"]}
        for field in ("nse_code", "bse_code", *LEVELS):
            if row[field]:
                values[field] = row[field]
        prior = set_this_load.setdefault(isin, {})
        for field, value in values.items():
            if field in prior and prior[field] != value:
                raise ValueError(
                    f"line {lineno}: conflicting {field} for ISIN {isin}: "
                    f"{prior[field]!r} vs {value!r}"
                )
            prior[field] = value
            setattr(inst, field, value)
        inst.classification_status = "classified" if inst.basic_industry else "missing"

    session.flush()
    return len(seen)
