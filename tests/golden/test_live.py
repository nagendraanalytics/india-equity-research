"""LIVE acceptance run for the IT slice: real model server, real exchange data, real database.

Skipped unless IER_LIVE=1, so it never runs in the default suite. It does NOT use any stub and
does NOT wipe anything: it works on the database named by IER_DATABASE_URL, which must already
be migrated, loaded (python -m ier.runtime load-classification ...) and have its sources
configured. See tests/golden/RUNBOOK.md. Needs the model server from config/models.yaml and
the exchange filing access configured in src/ier/tools/config/exchange_filings.yaml.

    IER_LIVE=1 IER_DATABASE_URL=postgresql+psycopg://postgres@localhost:5432/ier \
        pytest tests/golden/test_live.py -m live -s
"""
import os
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.engine import make_url

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.environ.get("IER_LIVE") != "1", reason="live run: set IER_LIVE=1"),
]

MACRO = os.environ.get("IER_LIVE_MACRO", "Information Technology")


def test_live_it_slice_end_to_end():
    from ier.db import session as db_session
    from ier.db.models import Instrument, Scorecard, Signal
    from ier.runtime.events import seed_agents
    from tests.golden import harness

    url = make_url(db_session.database_url())
    if (url.database or "").endswith("_test"):
        pytest.fail("IER_DATABASE_URL points at a *_test database; use your real one")
    factory = db_session.get_session
    with factory() as s:
        seed_agents(s, MACRO)
        s.commit()
    from ier.graph.company import register_default_handlers
    from ier.runtime.worker import HANDLERS
    handlers = register_default_handlers(HANDLERS, overwrite=False)
    run_id = f"live-{uuid.uuid4().hex[:8]}"
    summary = harness.run_pipeline(factory, MACRO, run_id, handlers=handlers,
                                   timeout=float(os.environ.get("IER_LIVE_TIMEOUT", "7200")))
    print("\njobs:", summary)
    with factory() as s:
        print(f"{'company':34} {'long':>6} {'short':>6} {'conf':>7}  rating / red flags")
        for inst in s.scalars(select(Instrument).where(Instrument.macro_sector == MACRO)
                              .order_by(Instrument.name)):
            card = s.scalars(select(Scorecard).where(Scorecard.instrument_id == inst.id)
                             .order_by(Scorecard.run_date.desc(), Scorecard.id.desc())).first()
            sig = s.scalars(select(Signal).where(Signal.instrument_id == inst.id)
                            .order_by(Signal.run_date.desc(), Signal.id.desc())).first()
            if card is None:
                print(f"{inst.name[:34]:34} (no scorecard)")
                continue
            fmt = lambda v: "-" if v is None else f"{v:.1f}"  # noqa: E731
            print(f"{inst.name[:34]:34} {fmt(card.long_term_score):>6} {fmt(card.short_term_score):>6} "
                  f"{card.confidence:>7}  {sig.rating if sig else '-'} {card.red_flags or ''}")
            assert harness.sourcing_problems(s, card) == [], inst.name
        assert harness.run_state_problems(s) == []
    assert not summary["timed_out"]
