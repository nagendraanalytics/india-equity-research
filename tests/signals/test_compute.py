import copy
import itertools
import math
import os
import re
from datetime import date
from pathlib import Path

import pytest
import yaml
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, select, text

from ier.agents.verifier import _denylist_re, load_config as load_verifier_config
from ier.db.models import Instrument, Scorecard, Signal
from ier.rules.engine import RuleResult
from ier.signals import compute as sc
from ier.signals.compute import (
    DEFAULT_PATH, RATINGS, compute_signal, load_signal_rules, persist_signal, signal_hook,
    validate_config,
)
from tests.db.conftest import ROOT, TEST_URL, assert_safe_test_db

D = date(2026, 11, 6)
_n = 0


def inst(session):
    global _n
    _n += 1
    i = Instrument(isin=f"INS{_n:09d}", name=f"Sig {_n}", macro_sector="IT", sector="Software",
                   industry="IT Services", status="active", listing_date=date(2020, 1, 1),
                   avg_daily_value=5e7)
    session.add(i)
    session.flush()
    return i


def card(long=70.0, short=70.0, conf="high", flags=(), iid=1, run_date=D, cid=None):
    return Scorecard(id=cid, instrument_id=iid, run_date=run_date, long_term_score=long,
                     short_term_score=short, confidence=conf, red_flags=list(flags))


def rr(long=70.0, short=70.0, flags=()):
    return RuleResult(sub_scores={}, long_term=long, short_term=short, red_flags=list(flags),
                      rules_version="it-test")


def sig(long=70.0, short=70.0, conf="high", flags=()):
    return compute_signal(card(long, short, conf, flags), rr(long, short, flags), DEFAULT_PATH)


def write_cfg(tmp_path, mutate=None):
    cfg = yaml.safe_load(DEFAULT_PATH.read_text())
    if mutate:
        mutate(cfg)
    p = tmp_path / "signals.yaml"
    p.write_text(yaml.safe_dump(cfg))
    return p


# --- the five brief tests ------------------------------------------------------------------

def test_red_flag_caps_rating_at_hold_or_below():
    order = ["avoid", "reduce", "hold", "accumulate"]
    for long, short in [(95, 95), (70, 70), (60, 55), (40, 40), (10, 10)]:
        plain = sig(long, short)
        flagged = sig(long, short, flags=["debt_equity 3.0 above 2.0"])
        assert order.index(flagged.rating) <= order.index("hold")
        assert order.index(flagged.rating) <= order.index(plain.rating)  # never raised
    assert sig(95, 95).rating == "accumulate"
    assert sig(95, 95, flags=["x"]).rating == "hold"
    assert any("cap" in r.lower() for r in sig(95, 95, flags=["x"]).reasons)


def test_low_confidence_gives_insufficient_evidence_and_no_other_rating():
    for conf in ("low", None):
        for long, short in [(99, 99), (50, 50), (0, 0), (None, 80)]:
            s = sig(long, short, conf=conf, flags=["x"])
            assert s.rating == "insufficient evidence"
    assert sig(99, 99, conf="medium").rating == "accumulate"


def test_same_inputs_same_rating():
    a = sig(63.3, 41.2, "medium", ["f"])
    b = sig(63.3, 41.2, "medium", ["f"])
    assert (a.rating, a.reasons, a.rules_version) == (b.rating, b.reasons, b.rules_version)


@pytest.mark.parametrize("conf", ["low", "medium", "high", None])
@pytest.mark.parametrize("flags", [[], ["flag one"], ["a", "b"]])
@pytest.mark.parametrize("long,short", list(itertools.product(
    [None, 0.0, 29.99, 30.0, 49.99, 50.0, 69.99, 70.0, 100.0, float("nan")],
    [None, 0.0, 30.0, 50.0, 70.0, 100.0])))
def test_rating_always_from_allowed_set(conf, flags, long, short):
    s = sig(long, short, conf, flags)
    assert s.rating in RATINGS
    assert isinstance(s.reasons, list) and s.reasons and all(isinstance(r, str) for r in s.reasons)


def test_signal_has_no_order_fields():
    cols = {c.name for c in Signal.__table__.columns}
    for banned in ("quantity", "price", "side", "order", "qty", "size"):
        assert banned not in cols
        assert not any(banned in c for c in cols), (banned, cols)


# --- bands, weights, None handling ---------------------------------------------------------

@pytest.mark.parametrize("score,expected", [
    (100, "accumulate"), (70, "accumulate"), (69.99, "hold"), (50, "hold"),
    (49.99, "reduce"), (30, "reduce"), (29.99, "avoid"), (0, "avoid")])
def test_band_boundaries_lower_edge_inclusive(score, expected):
    assert sig(score, score).rating == expected  # equal scores: weights do not matter


def test_weights_combine_long_and_short():
    # 0.6 * 100 + 0.4 * 25 = 70 -> accumulate (inclusive edge); 0.6*25+0.4*100 = 55 -> hold
    assert sig(100, 25).rating == "accumulate"
    assert sig(25, 100).rating == "hold"


def test_one_score_none_uses_other_alone():
    assert sig(None, 80).rating == "accumulate"
    assert sig(20, None).rating == "avoid"
    assert any("alone" in r for r in sig(None, 80).reasons)


@pytest.mark.parametrize("conf", ["Low", "unknown", "", " low", "HIGH", "medium "])
def test_unrecognised_confidence_is_insufficient(conf):
    s = sig(90, 90, conf=conf)
    assert s.rating == "insufficient evidence"
    assert any("not recognised" in r for r in s.reasons)


@pytest.mark.parametrize("args", [dict(conf="low"), dict(conf=None), dict(conf="bogus"),
                                  dict(long=None, short=None)])
def test_insufficient_reasons_have_no_band_or_score_text(args):
    s = sig(**{"long": 90, "short": 90, **args})
    assert s.rating == "insufficient evidence"
    text_ = " ".join(s.reasons).lower()
    assert "band" not in text_ and "combined" not in text_ and "accumulate" not in text_
    assert not re.search(r"\d+\.\d", text_)


def test_both_scores_none_is_insufficient():
    s = sig(None, None)
    assert s.rating == "insufficient evidence"
    assert sig(None, None, conf="high", flags=["x"]).rating == "insufficient evidence"


def test_red_flag_with_insufficient_evidence_stays_insufficient():
    assert sig(90, 90, conf="low", flags=["x"]).rating == "insufficient evidence"
    assert sig(None, None, flags=["x"]).rating == "insufficient evidence"


def test_out_of_range_scores_are_clamped_with_reason():
    hi = sig(150, 150)
    assert hi.rating == "accumulate" and any("clamped" in r for r in hi.reasons)
    lo = sig(-20, -20)
    assert lo.rating == "avoid" and any("clamped" in r for r in lo.reasons)
    assert not any("clamped" in r for r in sig(70, 70).reasons)
    assert not any("clamped" in r for r in sig(100, 0).reasons)
    inf = sig(float("inf"), 80)
    assert inf.rating == "accumulate" and any("alone" in r for r in inf.reasons)


def test_zero_weight_with_other_score_none_uses_remaining_score(tmp_path):
    p = write_cfg(tmp_path, lambda c: c["weights"].update(long=0, short=1))
    s = compute_signal(card(None, 80), rr(None, 80), p)
    assert s.rating == "accumulate"
    s = compute_signal(card(80, None), rr(80, None), p)
    assert s.rating == "accumulate"  # long used alone although its weight is 0


def test_signals_yaml_documents_zero_weight_and_verbatim_flags():
    raw = DEFAULT_PATH.read_text().lower()
    assert "weight of 0" in raw and "verbatim" in raw


def test_cap_does_not_raise_lower_ratings():
    assert sig(10, 10, flags=["x"]).rating == "avoid"
    assert sig(40, 40, flags=["x"]).rating == "reduce"


def test_signal_fields_and_rules_version():
    c = card(80, 80, iid=7, cid=3)
    s = compute_signal(c, rr(80, 80), DEFAULT_PATH)
    assert (s.instrument_id, s.scorecard_id, s.run_date) == (7, 3, D)
    assert s.rules_version == load_signal_rules()["version"]


def test_red_flag_cap_configurable(tmp_path):
    p = write_cfg(tmp_path, lambda c: c["red_flag_cap"].update(max_rating="reduce"))
    s = compute_signal(card(95, 95, flags=["x"]), rr(95, 95, ["x"]), p)
    assert s.rating == "reduce"
    s = compute_signal(card(55, 55, flags=["x"]), rr(55, 55, ["x"]), p)
    assert s.rating == "reduce"


# --- wording -------------------------------------------------------------------------------

def _deny():
    cv = load_verifier_config()["verifier"]
    return _denylist_re(cv["denylist"], cv["rating_words"])


def test_reasons_and_config_pass_wording_denylist():
    deny = _deny()
    texts = []
    for long, short, conf, flags in itertools.product(
            [None, 0, 40, 60, 95], [None, 20, 95], ["low", "medium", None],
            [[], ["Debt to equity 3.0 is above 2.0"]]):
        texts += sig(long, short, conf, flags).reasons
    assert texts
    for t in texts:
        assert not deny.search(t), t
    raw = DEFAULT_PATH.read_text()
    assert not deny.search(raw)
    comments = " ".join(m.group(1) for m in re.finditer(r"#\s*(.*)", raw))
    assert not deny.search(comments)
    assert "uncalibrated" in raw.lower()


# --- config validation ---------------------------------------------------------------------

def test_default_config_valid():
    assert load_signal_rules()["version"]


@pytest.mark.parametrize("mutate,needle", [
    (lambda c: c.pop("version"), "version"),
    (lambda c: c.pop("confidence_gate"), "confidence_gate"),
    (lambda c: c["confidence_gate"].update(insufficient_confidence=["bogus"]), "insufficient_confidence"),
    (lambda c: c["weights"].pop("long"), "weights.long"),
    (lambda c: c["weights"].update(short=-1), "weights"),
    (lambda c: c["weights"].update(long=0, short=0), "positive sum"),
    (lambda c: c["weights"].update(long="x"), "weights.long"),
    (lambda c: c["bands"].update(hold_min=float("nan")), "bands.hold_min"),
    (lambda c: c["bands"].update(reduce_min=float("inf")), "bands.reduce_min"),
    (lambda c: c["bands"].update(hold_min=70), "strictly ordered"),
    (lambda c: c["bands"].update(reduce_min=60), "strictly ordered"),
    (lambda c: c["bands"].pop("accumulate_min"), "bands.accumulate_min"),
    (lambda c: c["bands"].update(accumulate_min=True), "bands.accumulate_min"),
    (lambda c: c["red_flag_cap"].update(max_rating="accumulate"), "max_rating"),
    (lambda c: c["red_flag_cap"].update(max_rating="insufficient evidence"), "max_rating"),
    (lambda c: c.pop("red_flag_cap"), "red_flag_cap"),
])
def test_config_validation_errors_name_key(mutate, needle):
    cfg = yaml.safe_load(DEFAULT_PATH.read_text())
    mutate(cfg)
    with pytest.raises(ValueError, match=re.escape(needle)):
        validate_config(copy.deepcopy(cfg), "x.yaml")


def test_invalid_file_rejected_at_load(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text("")
    with pytest.raises(ValueError):
        load_signal_rules(p)
    p = write_cfg(tmp_path, lambda c: c["bands"].update(hold_min=99))
    with pytest.raises(ValueError, match="strictly ordered"):
        compute_signal(card(), rr(), p)


# --- persistence ---------------------------------------------------------------------------

def _stored_card(session, i, **kw):
    c = Scorecard(instrument_id=i.id, run_date=kw.pop("run_date", D),
                  long_term_score=kw.pop("long", 80.0), short_term_score=kw.pop("short", 80.0),
                  confidence=kw.pop("conf", "high"), red_flags=kw.pop("flags", []),
                  sub_scores={}, rules_version="it-test")
    session.add(c)
    session.flush()
    return c


def test_persist_signal_stores_reasons_and_flushes(session):
    i = inst(session)
    c = _stored_card(session, i)
    s = persist_signal(session, c, compute_signal(c, rr(80, 80), DEFAULT_PATH))
    assert s.id is not None
    session.expire_all()
    got = session.get(Signal, s.id)
    assert got.rating == "accumulate" and got.reasons and got.scorecard_id == c.id


def test_persist_signal_replaces_stale_signal(session):
    i = inst(session)
    c = _stored_card(session, i)
    first = persist_signal(session, c, compute_signal(c, rr(80, 80), DEFAULT_PATH))
    c.long_term_score = c.short_term_score = 10.0
    second = persist_signal(session, c, compute_signal(c, rr(10, 10), DEFAULT_PATH))
    rows = session.scalars(select(Signal).where(Signal.instrument_id == i.id)).all()
    assert [r.id for r in rows] == [second.id] and rows[0].rating == "avoid"
    assert first.id != second.id


def test_persist_signal_replaces_same_instrument_run_date_and_keeps_others(session):
    i, j = inst(session), inst(session)
    c = _stored_card(session, i)
    old = Signal(instrument_id=i.id, rating="hold", scorecard_id=None, rules_version="v0",
                 run_date=D)
    other_day = _stored_card(session, i, run_date=date(2026, 11, 5))
    keep = persist_signal(session, other_day, compute_signal(other_day, rr(), DEFAULT_PATH))
    cj = _stored_card(session, j)
    keepj = persist_signal(session, cj, compute_signal(cj, rr(), DEFAULT_PATH))
    session.add(old)
    session.flush()
    persist_signal(session, c, compute_signal(c, rr(80, 80), DEFAULT_PATH))
    ids = {s.id for s in session.scalars(select(Signal))}
    assert old.id not in ids and keep.id in ids and keepj.id in ids


def test_signal_hook_builds_from_scorecard(session):
    i = inst(session)
    c = _stored_card(session, i, long=90.0, short=90.0, flags=["Debt to equity 3.0 above 2.0"])
    signal_hook(session, i.id, c.id)
    s = session.scalars(select(Signal).where(Signal.scorecard_id == c.id)).one()
    assert s.rating == "hold" and s.reasons
    signal_hook(session, i.id, c.id)  # idempotent
    assert len(session.scalars(select(Signal).where(Signal.scorecard_id == c.id)).all()) == 1


def test_signal_hook_unknown_scorecard(session):
    i = inst(session)
    with pytest.raises(ValueError):
        signal_hook(session, i.id, 999999)


# --- migration -----------------------------------------------------------------------------

def test_migration_0008_up_down():
    assert_safe_test_db(TEST_URL)
    base = TEST_URL.rsplit("/", 1)[0]
    url = f"{base}/ier_migration_test_0008"
    admin = create_engine(f"{base}/postgres", isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text("DROP DATABASE IF EXISTS ier_migration_test_0008 WITH (FORCE)"))
        c.execute(text("CREATE DATABASE ier_migration_test_0008"))
    eng = create_engine(url)
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    cfg.set_main_option("prepend_sys_path", str(ROOT / "src"))
    previous = os.environ.get("IER_DATABASE_URL")
    os.environ["IER_DATABASE_URL"] = url

    def cols(table):
        return {c["name"]: c for c in inspect(eng).get_columns(table)}
    try:
        command.upgrade(cfg, "0007")
        assert "reasons" not in cols("signals") and "rules_version" not in cols("scorecards")
        command.upgrade(cfg, "0008")
        assert cols("signals")["reasons"]["nullable"] and cols("scorecards")["rules_version"]["nullable"]
        command.downgrade(cfg, "0007")
        assert "reasons" not in cols("signals") and "rules_version" not in cols("scorecards")
        command.upgrade(cfg, "head")
        assert "reasons" in cols("signals")
    finally:
        if previous is None:
            os.environ.pop("IER_DATABASE_URL", None)
        else:
            os.environ["IER_DATABASE_URL"] = previous
        eng.dispose()
        with admin.connect() as c:
            c.execute(text("DROP DATABASE IF EXISTS ier_migration_test_0008 WITH (FORCE)"))
