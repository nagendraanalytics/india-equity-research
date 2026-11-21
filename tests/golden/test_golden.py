"""IT slice acceptance run on a SYNTHETIC fixture (see it_golden.yaml header and RUNBOOK.md).

The whole pipeline runs once per module (real queue, workers, graph, rules, verifier, signals;
stub model, network and embedder). The ranges are a regression guard of the placeholder rules,
not a validation of investment quality, and the real hand-checked set is still an open item.
"""
import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import sessionmaker

from ier.db.models import (AgentEvent, Fact, Instrument, Job, Scorecard, Signal, Source)
from ier.db.models import Agent, Base
from ier.signals.compute import DEFAULT_PATH, compute_signal
from ier.rules.engine import RuleResult
from tests.golden import harness

pytestmark = pytest.mark.filterwarnings("ignore")


def _wipe(engine):
    names = ", ".join(t.name for t in Base.metadata.sorted_tables)
    with engine.begin() as c:
        c.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))


@pytest.fixture(scope="module")
def golden():
    return harness.load_golden()


@pytest.fixture(scope="module")
def run(engine, golden, tmp_path_factory):
    _wipe(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    mp = pytest.MonkeyPatch()
    try:
        rec = harness.install_stubs(mp, golden, tmp_path_factory.mktemp("cache"))
        handlers = harness.make_handlers(mp)
        harness.seed_world(factory, golden)
        summary = harness.run_pipeline(factory, golden["macro_sector"], "golden-run-1",
                                       handlers=handlers)
        yield {"factory": factory, "summary": summary, **rec}
    finally:
        mp.undo()
        _wipe(engine)


def _within(value, rng, what):
    if rng is None:
        assert value is None, f"{what}: expected no score, got {value}"
    else:
        assert value is not None, f"{what}: expected {rng}, got None"
        lo, hi = rng
        assert lo <= value <= hi, f"{what}: {value:.3f} outside [{lo}, {hi}]"


def _card(s, co):
    inst = s.scalars(select(Instrument).where(Instrument.isin == co["isin"])).one()
    return inst, s.scalars(select(Scorecard).where(Scorecard.instrument_id == inst.id)).one()


def test_pipeline_ran_cleanly(run, golden):
    assert run["summary"]["timed_out"] is False
    assert run["summary"]["failed"] == 0 and run["summary"]["queued"] == 0
    assert run["summary"]["done"] == 1 + 1 + len(golden["companies"])  # sector, industry, companies
    assert set(run["hosts"]) == {harness.FILINGS_HOST}  # nothing else was ever contacted


def test_golden_scores_within_ranges(run, golden):
    with run["factory"]() as s:
        tailwind = [e.payload for e in s.scalars(select(AgentEvent).where(AgentEvent.kind == "output"))
                    if isinstance(e.payload, dict) and e.payload.get("kind") == "sector_view"]
        assert len(tailwind) == 1
        assert tailwind[0]["tailwind_input"] == golden["sector"]["expected_tailwind"]
        for co in golden["companies"]:
            exp, name = co["expected"], co["name"]
            inst, card = _card(s, co)
            for sub, rng in exp["sub_scores"].items():
                _within(card.sub_scores[sub], rng, f"{name} {sub}")
            _within(card.long_term_score, exp["long_term"], f"{name} long_term")
            _within(card.short_term_score, exp["short_term"], f"{name} short_term")
            assert card.red_flags == exp["red_flags"], name
            assert card.confidence == exp["confidence"], name
            sig = s.scalars(select(Signal).where(Signal.instrument_id == inst.id)).one()
            assert sig.rating == exp["rating"], f"{name}: {sig.rating!r}"
            assert sig.scorecard_id == card.id
            # the stored combined score (quoted in the reasons) is inside the hand-derived range
            if exp["confidence"] != "low":
                import re
                combined = float(re.search(r"Combined score ([\d.]+)", sig.reasons[0]).group(1))
                _within(combined, exp["combined"], f"{name} combined")
            # research wording only: no order language anywhere in the reasons
            for reason in sig.reasons:
                assert not harness.ORDER_WORDING.search(reason), f"{name}: {reason!r}"


def test_red_flag_caps_an_otherwise_accumulate_score_at_hold(run, golden):
    four = next(c for c in golden["companies"] if c["key"] == "four")
    with run["factory"]() as s:
        inst, card = _card(s, four)
        sig = s.scalars(select(Signal).where(Signal.instrument_id == inst.id)).one()
        assert sig.rating == "hold"
        assert any("Red flag cap applied" in r for r in sig.reasons)
        # Without the flag the very same scorecard maps to the uncapped band (real signal rules).
        rr = RuleResult(sub_scores=dict(card.sub_scores), long_term=card.long_term_score,
                        short_term=card.short_term_score, red_flags=[], rules_version="x")
        assert compute_signal(card, rr, DEFAULT_PATH).rating == four["expected"]["uncapped_rating"]
        # and the red flag text is quoted verbatim from the rules engine
        assert any(card.red_flags[0] in r for r in sig.reasons)


def test_thin_evidence_is_gated_whatever_the_score(run, golden):
    five = next(c for c in golden["companies"] if c["key"] == "five")
    with run["factory"]() as s:
        _, card = _card(s, five)
        assert card.long_term_score > 70  # would be accumulate if the gate did not apply
        assert card.confidence == "low"


def test_every_claim_has_source(run, golden):
    with run["factory"]() as s:
        # facts never rest on a Tier 3 source, and the careless Tier-3 extraction was refused
        for f in s.scalars(select(Fact)):
            assert s.get(Source, f.source_id).tier in (1, 2), f"fact {f.metric}={f.value}"
        assert not s.scalars(select(Fact).where(Fact.metric == "promoter_pledging",
                                                Fact.value == 35)).first()
        for co in golden["companies"]:
            inst, card = _card(s, co)
            expected_facts = sorted((f["metric"], float(f["value"])) for f in co["facts"]
                                    if f.get("expect_stored", True))
            stored = sorted((f.metric, f.value) for f in s.scalars(
                select(Fact).where(Fact.instrument_id == inst.id)))
            assert stored == expected_facts, co["name"]

            assert harness.sourcing_problems(s, card) == [], co["name"]
            published = {c["text"] for c in harness.published_claims(s, card)}
            kept = {c["text"] for c in co["claims"] if c["expect"] == "kept"}
            dropped = {c["text"] for c in co["claims"] if c["expect"] == "dropped"}
            assert published == kept, co["name"]
            assert not published & dropped, co["name"]
            # the synthesis worker reports what the verifier dropped
            synth = s.scalars(select(Agent).where(Agent.scope_instrument_id.is_(None),
                                                  Agent.role == "synthesis",
                                                  Agent.parent_id == s.scalars(select(Agent.id).where(
                                                      Agent.scope_instrument_id == inst.id,
                                                      Agent.role == "company")).one())).one()
            out = [e.payload for e in s.scalars(select(AgentEvent).where(
                AgentEvent.agent_id == synth.id, AgentEvent.kind == "output"))]
            assert out and out[-1]["dropped"] == len(dropped) and out[-1]["kept"] == len(kept)


def test_verifier_actually_dropped_the_bad_claims_for_the_right_reasons(run, golden):
    """Re-run the real verifier over the drafted claims and check each drop reason."""
    from ier.agents.synthesis import Claim
    from ier.agents.verifier import verify
    from ier.db.models import Chunk, Document
    with run["factory"]() as s:
        for co in golden["companies"]:
            inst, _ = _card(s, co)
            chunks = s.scalars(select(Chunk).join(Document, Chunk.document_id == Document.id)
                               .where(Document.instrument_id == inst.id)).all()
            claims = []
            for c in co["claims"]:
                ids = [ch.id for needle in c["cite"] for ch in chunks if needle in ch.text][:len(c["cite"])]
                claims.append(Claim(kind=c["kind"], text=c["text"], severity=c["severity"],
                                    horizon=c["horizon"], evidence_ids=ids))
            # the model-check verdicts are the stub's, so route them through it again
            result = verify(claims, s, instrument_id=inst.id, run_id="golden-reverify")
            reasons = {c.text: r for c, r in result.dropped}
            for c in co["claims"]:
                if c["expect"] == "dropped":
                    assert c["text"] in reasons, (co["name"], c["text"])
                    assert c["reason_contains"] in reasons[c["text"]], reasons[c["text"]]
                else:
                    assert c["text"] not in reasons, (co["name"], c["text"], reasons.get(c["text"]))


def test_full_run_leaves_no_agent_in_running_state(run):
    with run["factory"]() as s:
        assert harness.run_state_problems(s) == []
        assert s.scalars(select(AgentEvent).where(AgentEvent.kind == "failed")).first() is None
        assert {j.status for j in s.scalars(select(Job))} == {"done"}
