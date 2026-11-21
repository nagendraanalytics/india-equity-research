"""Seed a LOCAL DEMO database for the dashboard (never run against real data).

Usage:  IER_DATABASE_URL=postgresql+psycopg://postgres@localhost:5433/ier_demo \
        python scripts/seed_demo.py
The database must exist and be migrated (alembic upgrade head). Refuses unless the database name ends
with '_demo' and the host is localhost or 127.0.0.1.
"""
import os
import sys
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.engine import make_url

from ier.db.models import (Agent, EvidenceLink, Fact, Instrument, RunLog, Scorecard, Signal,
                           Source)
from ier.db.session import database_url, get_session
from ier.runtime.events import emit, seed_agents
from ier.runtime.queue import enqueue

MACRO = "Information Technology"
TODAY = date.today()


def assert_demo_db(url: str) -> None:
    u = make_url(url)
    name = u.database or ""
    if u.query:  # libpq lets ?host=/?dbname=/?service= override the URL's host and database
        sys.exit("refusing to seed: the database URL must not carry query parameters")
    if not name.endswith("_demo") or u.host not in ("localhost", "127.0.0.1"):
        sys.exit(f"refusing to seed {name!r} on host {u.host!r}: the database name must end with "
                 "'_demo' and the host must be localhost or 127.0.0.1")


def main() -> None:
    assert_demo_db(database_url())
    with get_session() as s:
        rows = [("INE000A01011", "Alpha Ltd", "Software", "IT Services", 72.5, 55.0, "high", "accumulate", 0),
                ("INE000B01012", "Beta Ltd", "Software", "IT Services", 58.0, 61.0, "medium", "hold", 12),
                ("INE000C01013", "Gamma Ltd", "Hardware", "Devices", 40.0, 35.0, "low", "insufficient evidence", 1),
                ("INE000D01014", "Delta Ltd", "Software", "Products", 33.0, 29.0, "medium", "reduce", 0)]
        insts = []
        for isin, nm, sec, ind, lt, st, conf, rating, age in rows:
            i = Instrument(isin=isin, name=nm, macro_sector=MACRO, sector=sec, industry=ind,
                           nse_code=nm[:5].upper(), status="active", listing_date=date(2020, 1, 1),
                           avg_daily_value=5e7)
            s.add(i)
            s.flush()
            insts.append((i, lt, st, conf, rating, age))
        seed_agents(s, MACRO)
        s.flush()
        src = Source(url="https://www.bseindia.com/demo/alpha-fy25.pdf", type="filing", tier=1,
                     publish_date=date(2025, 5, 10), fetch_date=datetime.now(timezone.utc))
        s.add(src)
        s.flush()
        for i, lt, st, conf, rating, age in insts:
            sc = Scorecard(instrument_id=i.id, run_date=TODAY - timedelta(days=age), long_term_score=lt,
                           short_term_score=st, confidence=conf, rules_version="demo-v1",
                           sub_scores={"growth": lt, "quality": st}, benefits=["Demo benefit"],
                           risks=["Demo risk"], red_flags=["Demo red flag"] if rating == "reduce" else [])
            s.add(sc)
            s.flush()
            s.add(Signal(instrument_id=i.id, rating=rating, scorecard_id=sc.id, rules_version="demo-v1",
                         run_date=sc.run_date, reasons=[f"demo reason for {rating}"]))
            if i.isin == "INE000A01011":
                f = Fact(instrument_id=i.id, source_id=src.id, metric="revenue", value=1200.5,
                         unit="INR cr", period="FY25", period_end=date(2025, 3, 31))
                s.add(f)
                s.flush()
                s.add(EvidenceLink(scorecard_id=sc.id, claim_type="benefit", claim_ref="revenue",
                                   fact_id=f.id, source_id=src.id))
        s.flush()
        company = {a.scope_instrument_id: a for a in s.scalars(select(Agent).where(Agent.role == "company"))}
        alpha, beta = company[insts[0][0].id], company[insts[1][0].id]
        s.add(RunLog(agent_id=alpha.id, inputs={"demo": True}, outputs={"status": "completed"},
                     run_id="demo-run-1", role="company", model_version="demo-model",
                     prompt_tokens=120, completion_tokens=60, model_seconds=2.5))
        emit(s, alpha.id, "started", {"task": "demo run"})
        emit(s, beta.id, "failed", {"error": "demo failure"})
        enqueue(s, company[insts[2][0].id].id, "manual")
        s.commit()
    print("seeded demo data")


if __name__ == "__main__":
    main()
