import pytest

from ier.agents import verifier
from ier.agents.synthesis import Claim
from ier.agents.verifier import Verdict, verify
from ier.llm.router import BudgetExceeded, SchemaError
from tests.agents.helpers import make_chunk, make_instrument

TEXT = (
    "Revenue from operations reached Rs 41,764 crore driven by large deal wins "
    "and healthy margin across digital services."
)
GOOD = "Revenue reached 41,764 crore driven by large deal wins"


def claim(text=GOOD, ids=(), kind="benefit", severity=3, horizon="long"):
    return Claim(kind=kind, text=text, severity=severity, horizon=horizon, evidence_ids=list(ids))


@pytest.fixture
def inst(session):
    return make_instrument(session)


@pytest.fixture
def no_model(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("model must not be called")
    monkeypatch.setattr(verifier, "complete", boom)


def reasons(res):
    return [r for _, r in res.dropped]


def test_claim_without_evidence_dropped(session, inst, no_model):
    res = verify([claim(ids=[])], session, instrument_id=inst.id)
    assert res.kept == [] and "no evidence" in reasons(res)[0]


def test_claim_not_supported_by_cited_chunk_dropped(session, inst, no_model):
    ch = make_chunk(session, inst, TEXT)
    c = claim("Company plans massive overseas acquisition of retail chains", [ch.id])
    res = verify([c], session, instrument_id=inst.id)
    assert res.kept == [] and "not supported" in reasons(res)[0]


def test_supported_claim_kept(session, inst, no_model):
    ch = make_chunk(session, inst, TEXT)
    res = verify([claim(ids=[ch.id])], session, instrument_id=inst.id)
    assert len(res.kept) == 1 and res.dropped == []


def test_tier3_source_cannot_support_claim(session, inst, no_model):
    ch = make_chunk(session, inst, TEXT, tier=3)
    res = verify([claim(ids=[ch.id])], session, instrument_id=inst.id)
    assert res.kept == [] and "tier" in reasons(res)[0]


def test_tier3_mixed_with_tier1_still_dropped(session, inst, no_model):
    a = make_chunk(session, inst, TEXT, tier=1)
    b = make_chunk(session, inst, TEXT + " blog", tier=3)
    res = verify([claim(ids=[a.id, b.id])], session, instrument_id=inst.id)
    assert res.kept == [] and "tier" in reasons(res)[0]


def test_tier2_allowed(session, inst, no_model):
    ch = make_chunk(session, inst, TEXT, tier=2)
    assert len(verify([claim(ids=[ch.id])], session, instrument_id=inst.id).kept) == 1


def test_missing_evidence_id_dropped(session, inst, no_model):
    res = verify([claim(ids=[999999])], session, instrument_id=inst.id)
    assert res.kept == [] and "evidence" in reasons(res)[0]


def test_cross_instrument_evidence_dropped(session, inst, no_model):
    other = make_instrument(session, "INE002A01018", "Reliance")
    ch = make_chunk(session, other, TEXT)
    res = verify([claim(ids=[ch.id])], session, instrument_id=inst.id)
    assert res.kept == [] and "another instrument" in reasons(res)[0]


@pytest.mark.parametrize("text", [
    "Buy this stock now, revenue grew strongly to 41,764 crore",
    "You should note revenue grew strongly to 41,764 crore",
    "Revenue grew strongly to 41,764 crore so SELL before results",
    "Place an order as revenue grew strongly to 41,764 crore",
    "Revenue grew strongly to 41,764 crore; short it",
    "Investors should note revenue grew strongly to 41,764 crore",
    "Target price rises as revenue grew strongly to 41,764 crore",
    "Analysts see the stock outperform as revenue grew strongly to 41,764 crore",
    "Hold the stock as revenue grew strongly to 41,764 crore",
    "Reduce exposure as revenue grew strongly to 41,764 crore",
    "Avoid the stock: revenue grew strongly to 41,764 crore",
    "Stay invested as revenue grew strongly to 41,764 crore",
    "Accumulate on revenue grew strongly to 41,764 crore",
])
def test_claims_contain_no_order_or_instruction_wording(session, inst, no_model, text):
    ch = make_chunk(session, inst, TEXT)
    res = verify([claim(text, [ch.id])], session, instrument_id=inst.id)
    assert res.kept == [] and "wording" in reasons(res)[0]


def test_denylist_matches_word_boundaries(session, inst, no_model):
    ch = make_chunk(session, inst, "Shortage of staff and seller concentration weigh on margin outlook.")
    c = claim("Shortage of staff and seller concentration weigh on margin outlook", [ch.id])
    assert len(verify([c], session, instrument_id=inst.id).kept) == 1


@pytest.mark.parametrize("kw", [{"severity": 0}, {"severity": 6}, {"horizon": "medium"}, {"kind": "tip"}])
def test_invalid_fields_dropped(session, inst, no_model, kw):
    ch = make_chunk(session, inst, TEXT)
    res = verify([claim(ids=[ch.id], **kw)], session, instrument_id=inst.id)
    assert res.kept == [] and len(res.dropped) == 1


BORDER_TEXT = "Revenue grew to Rs 41,764 crore. Management discussed deal pipeline and hiring plans."
BORDER = "Revenue grew with a strong deal pipeline and attrition falling"  # partial overlap


def _borderline(session, inst):
    ch = make_chunk(session, inst, BORDER_TEXT)
    return claim(BORDER, [ch.id])


def test_borderline_model_check_supported_kept(session, inst, monkeypatch):
    calls = []

    def fake(role, prompt, schema, run_id, *, session, **kw):
        calls.append((role, prompt, schema))
        return Verdict(supported=True)

    monkeypatch.setattr(verifier, "complete", fake)
    c = _borderline(session, inst)
    assert 0.4 <= verifier.overlap(c, session) < 0.75
    res = verify([c], session, instrument_id=inst.id)
    assert len(res.kept) == 1
    assert calls and calls[0][0] == "extract" and calls[0][2] is Verdict
    assert "UNTRUSTED" in calls[0][1]


def test_borderline_model_check_unsupported_dropped(session, inst, monkeypatch):
    monkeypatch.setattr(verifier, "complete", lambda *a, **k: Verdict(supported=False))
    res = verify([_borderline(session, inst)], session, instrument_id=inst.id)
    assert res.kept == [] and "not supported" in reasons(res)[0]


def test_borderline_schema_error_fails_closed(session, inst, monkeypatch):
    def bad(*a, **k):
        raise SchemaError("nope")
    monkeypatch.setattr(verifier, "complete", bad)
    res = verify([_borderline(session, inst)], session, instrument_id=inst.id)
    assert res.kept == [] and "unavailable" in reasons(res)[0]


def test_borderline_budget_exceeded_propagates(session, inst, monkeypatch):
    def bad(*a, **k):
        raise BudgetExceeded("cap")
    monkeypatch.setattr(verifier, "complete", bad)
    with pytest.raises(BudgetExceeded):
        verify([_borderline(session, inst)], session, instrument_id=inst.id)


def test_bad_config_names_key(tmp_path):
    p = tmp_path / "s.yaml"
    p.write_text("verifier:\n  overlap_low: 0.9\n  overlap_high: 0.5\n  denylist: [buy]\n"
                 "confidence: {min_evidence_chunks: 1, max_none_sub_scores: 1, "
                 "high_evidence_chunks: 2, high_max_none_sub_scores: 0}\n")
    with pytest.raises(ValueError, match="overlap_low"):
        verifier.load_config(p)
    p.write_text("verifier:\n  overlap_low: 0.1\n  overlap_high: 0.5\nconfidence: {}\n")
    with pytest.raises(ValueError, match="denylist"):
        verifier.load_config(p)


@pytest.mark.parametrize("text", [
    "Short-term borrowings rose sharply during the quarter",
    "Buy-back of shares was completed during the quarter",
    "Company reduced debt and accumulated losses narrowed during the quarter",
    "Holding company structure simplified during the quarter",
])
def test_denylist_no_false_hits_on_normal_prose(session, inst, monkeypatch, text):
    _spy(monkeypatch, supported=True)  # some of these carry direction words: the model decides
    ch = make_chunk(session, inst, text + ".")
    res = verify([claim(text, [ch.id])], session, instrument_id=inst.id)
    assert len(res.kept) == 1, res.dropped


def test_increase_forms_match(session, inst, monkeypatch):
    calls = _spy(monkeypatch, supported=True)
    ch = make_chunk(session, inst, "Revenue increased and costs increases were contained, an increase overall")
    c = claim("Revenue increase and costs increased were contained overall", [ch.id])
    assert len(verify([c], session, instrument_id=inst.id).kept) == 1
    assert verifier.overlap(c, session) >= 0.75 and len(calls) == 1  # matched, but model-checked


def test_bad_severity_type_drops_only_that_claim(session, inst, no_model, monkeypatch):
    from ier.agents import synthesis
    ch = make_chunk(session, inst, TEXT)
    monkeypatch.setattr(synthesis, "complete", lambda role, p, schema, rid, **k: schema.model_validate({"claims": [
        {"kind": "benefit", "text": GOOD, "severity": "high", "horizon": "long", "evidence_ids": [ch.id]},
        {"kind": "benefit", "text": GOOD, "severity": 3.5, "horizon": "long", "evidence_ids": [ch.id]},
        {"kind": "benefit", "text": GOOD, "severity": 3, "horizon": "long", "evidence_ids": [ch.id]}]}))
    from tests.knowledge.conftest import fake_embed
    from ier.knowledge.retrieval import ingest_document
    from ier.rules.engine import RuleResult
    ingest_document(session, ch.source_id, TEXT, {"instrument_id": inst.id}, embed=fake_embed)
    cl = synthesis.draft_claims(session, inst.id, RuleResult({}, None, None, [], "v"), "r", embed=fake_embed)
    assert len(cl) == 3
    res = verify(cl, session, instrument_id=inst.id)
    assert len(res.kept) == 1 and len(res.dropped) == 2


# --- support gates: each probe must reach the model (or be dropped), never the fast path ---

def _spy(monkeypatch, supported=False):
    calls = []

    def fake(role, prompt, schema, run_id, *, session, **kw):
        calls.append(prompt)
        return Verdict(supported=supported)

    monkeypatch.setattr(verifier, "complete", fake)
    return calls


def test_negation_missing_from_text_goes_to_model(session, inst, monkeypatch):
    calls = _spy(monkeypatch)
    ch = make_chunk(session, inst, "Operating margins have expanded across all digital services segments")
    res = verify([claim("Operating margins have not expanded across all digital services segments", [ch.id])],
                 session, instrument_id=inst.id)
    assert res.kept == [] and len(calls) == 1 and "negation" in calls[0]


def test_negation_present_in_text_still_goes_to_model(session, inst, monkeypatch):
    calls = _spy(monkeypatch, supported=True)
    ch = make_chunk(session, inst, "Operating margins have not expanded across all digital services segments")
    c = claim("Operating margins have not expanded across all digital services segments", [ch.id])
    assert len(verify([c], session, instrument_id=inst.id).kept) == 1
    assert len(calls) == 1 and "negation or direction word present" in calls[0]


def test_single_digit_number_is_a_gate(session, inst, monkeypatch):
    # Without the number gate this claim overlaps 6/7 (>= overlap_high) and would be kept.
    calls = _spy(monkeypatch)
    ch = make_chunk(session, inst, "Net profit was 8 crore in the prior reporting quarter")
    c = claim("Net profit was 3 crore in the prior reporting quarter", [ch.id])
    assert verifier.overlap(c, session) >= 0.75
    res = verify([c], session, instrument_id=inst.id)
    assert res.kept == [] and len(calls) == 1 and "numbers not found" in calls[0]


def test_valid_claim_still_takes_fast_path(session, inst, no_model):
    ch = make_chunk(session, inst, "Net profit was 8 crore in the prior quarter. Costs were contained.")
    c = claim("Net profit was 8 crore in the prior quarter", [ch.id])
    assert len(verify([c], session, instrument_id=inst.id).kept) == 1


def test_plain_claim_zero_model_calls(session, inst, monkeypatch):
    calls = _spy(monkeypatch)
    ch = make_chunk(session, inst, TEXT)
    assert len(verify([claim(ids=[ch.id])], session, instrument_id=inst.id).kept) == 1
    assert calls == []


def test_number_mismatch_2_5_vs_25_goes_to_model(session, inst, monkeypatch):
    calls = _spy(monkeypatch)
    ch = make_chunk(session, inst, "Revenue grew 25% year over year in the reporting quarter")
    res = verify([claim("Revenue grew 2.5% year over year in the reporting quarter", [ch.id])],
                 session, instrument_id=inst.id)
    assert res.kept == [] and len(calls) == 1 and "2.5" in calls[0]


def test_number_normalisation_comma_percent_trailing_zero(session, inst, no_model):
    ch = make_chunk(session, inst, "Margin stood at 25.0 percent with revenue of Rs 41,764 crore in the quarter")
    c = claim("Margin stood at 25% with revenue of Rs 41764 crore in the quarter", [ch.id])
    assert len(verify([c], session, instrument_id=inst.id).kept) == 1


def test_short_claim_goes_to_model(session, inst, monkeypatch):
    calls = _spy(monkeypatch, supported=True)
    ch = make_chunk(session, inst, "Revenue grew")
    res = verify([claim("Revenue grew", [ch.id])], session, instrument_id=inst.id)
    assert len(calls) == 1 and "fewer than" in calls[0] and len(res.kept) == 1


def test_pieced_together_claim_goes_to_model(session, inst, monkeypatch):
    calls = _spy(monkeypatch)
    a = make_chunk(session, inst, "Revenue grew strongly this quarter on large deal wins")
    b = make_chunk(session, inst, "Attrition declined sharply while hiring accelerated across regions")
    c = claim("Revenue grew strongly on large deal wins while attrition declined sharply "
              "and hiring accelerated", [a.id, b.id])
    res = verify([c], session, instrument_id=inst.id)
    assert res.kept == [] and len(calls) == 1 and "combined" in calls[0]


def test_instrument_id_is_required(session, inst):
    with pytest.raises(TypeError):
        verify([], session)


POS = "Operating margins have expanded across all digital services segments"


@pytest.mark.parametrize("neg", [
    "haven't", "hasn't", "isn't", "aren't", "wasn't", "weren't", "didn't", "doesn't", "won't",
    "can't", "couldn't", "shouldn't", "wouldn't", "haven\u2019t", "can\u2019t", "cannot",
])
def test_contractions_count_as_negation(session, inst, monkeypatch, neg):
    calls = _spy(monkeypatch)
    ch = make_chunk(session, inst, POS)
    res = verify([claim(f"Operating margins {neg} expanded across all digital services segments", [ch.id])],
                 session, instrument_id=inst.id)
    assert res.kept == [] and len(calls) == 1 and "negation or direction word present" in calls[0]
    assert "not" in calls[0].split("present:")[1].split(".")[0]


def test_contraction_in_chunk_and_claim_goes_to_model(session, inst, monkeypatch):
    calls = _spy(monkeypatch, supported=True)
    ch = make_chunk(session, inst, "Operating margins haven\u2019t expanded across all digital services segments")
    c = claim("Operating margins have not expanded across all digital services segments", [ch.id])
    assert len(verify([c], session, instrument_id=inst.id).kept) == 1 and len(calls) == 1


def test_negated_chunk_non_negated_claim_goes_to_model(session, inst, monkeypatch):
    calls = _spy(monkeypatch)
    ch = make_chunk(session, inst, "Operating margins have not expanded across all digital services segments")
    res = verify([claim(POS, [ch.id])], session, instrument_id=inst.id)
    assert res.kept == [] and len(calls) == 1 and "negation or direction word present" in calls[0]


def test_negation_elsewhere_in_chunk_does_not_pass(session, inst, monkeypatch):
    calls = _spy(monkeypatch)
    ch = make_chunk(session, inst, "Revenue was not affected by currency. " + POS + ".")
    c = claim("Operating margins have not expanded across all digital services segments", [ch.id])
    res = verify([c], session, instrument_id=inst.id)
    assert res.kept == [] and len(calls) == 1


def test_negation_in_other_sentence_goes_to_model(session, inst, monkeypatch):
    # Ruling (fix round 4): fail closed at chunk level. Previously this took the fast path.
    calls = _spy(monkeypatch, supported=True)
    plain = "Operating margins stayed healthy across all digital services segments"
    ch = make_chunk(session, inst, "Revenue was not affected by currency. " + plain + ".")
    assert len(verify([claim(plain, [ch.id])], session, instrument_id=inst.id).kept) == 1
    assert len(calls) == 1 and "in cited chunk: not" in calls[0] and "in claim: none" in calls[0]


@pytest.mark.parametrize("claim_num,chunk_num", [("-3", "3"), ("3", "-3"), ("3", "(3)"), ("(3)", "3")])
def test_sign_is_part_of_number(session, inst, monkeypatch, claim_num, chunk_num):
    calls = _spy(monkeypatch)
    ch = make_chunk(session, inst, f"Operating profit change was {chunk_num} percent in the reporting quarter")
    c = claim(f"Operating profit change was {claim_num} percent in the reporting quarter", [ch.id])
    res = verify([c], session, instrument_id=inst.id)
    assert res.kept == [] and len(calls) == 1 and "numbers not found" in calls[0]


def test_same_signed_number_fast_path(session, inst, no_model):
    ch = make_chunk(session, inst, "Operating profit change was -3 percent in the reporting quarter")
    c = claim("Operating profit change was -3 percent in the reporting quarter", [ch.id])
    assert len(verify([c], session, instrument_id=inst.id).kept) == 1


@pytest.mark.parametrize("claim_text,chunk_text", [
    ("Net profit rose by 8 percent over the prior quarter", "Net profit declined by 8 percent over the prior quarter"),
    ("Net profit grew by 8 percent over the prior quarter", "Net profit fell by 8 percent over the prior quarter"),
    ("Net profit increased by 8 percent over the prior quarter", "Net profit fell by 8 percent over the prior quarter"),
    ("Operating margin improved by 8 percent over the prior quarter", "Operating margin worsened by 8 percent over the prior quarter"),
    ("Operating margin expanded by 8 percent over the prior quarter", "Operating margin failed to expand by 8 percent over the prior quarter"),
    ("All segments expanded by 8 percent over the prior quarter", "None of the segments expanded by 8 percent over the prior quarter"),
    ("Revenue did not grow 12 percent versus FY24 across the group", "Revenue grew 12 percent versus FY24 across the group"),
    ("Net profit exceeded Rs 500 crore in the prior year", "Net profit did not exceed Rs 500 crore in the prior year"),
])
def test_negation_or_direction_goes_to_model(session, inst, monkeypatch, claim_text, chunk_text):
    calls = _spy(monkeypatch)
    ch = make_chunk(session, inst, chunk_text)
    res = verify([claim(claim_text, [ch.id])], session, instrument_id=inst.id)
    assert res.kept == [] and len(calls) == 1
    assert "negation or direction word present" in calls[0]


def test_b1_probes_exact(session, inst, monkeypatch):
    calls = _spy(monkeypatch)
    ch = make_chunk(session, inst, "Revenue did not grow 12 percent vs. FY24 in the reporting year. Costs were flat.")
    verify([claim("Revenue grew 12 percent versus FY24 in the reporting year", [ch.id])],
           session, instrument_id=inst.id)
    ch2 = make_chunk(session, inst, "Net profit did not exceed Rs. 500 crore in the prior year.")
    verify([claim("Net profit exceeded Rs 500 crore in the prior year", [ch2.id])],
           session, instrument_id=inst.id)
    assert len(calls) == 2


def test_b2_probe_negation_in_other_clause(session, inst, monkeypatch):
    calls = _spy(monkeypatch)
    ch = make_chunk(session, inst,
                    "No dividend was declared and margins have expanded across all digital services segments")
    c = claim("Margins have not expanded across all digital services segments", [ch.id])
    res = verify([c], session, instrument_id=inst.id)
    assert res.kept == [] and len(calls) == 1


def test_negated_copy_elsewhere_in_chunk_goes_to_model(session, inst, monkeypatch):
    calls = _spy(monkeypatch)
    ch = make_chunk(session, inst, "Segments reported healthy margins across services. "
                    "Segments did not report healthy margins across services.")
    res = verify([claim("Segments reported healthy margins across services", [ch.id])],
                 session, instrument_id=inst.id)
    assert res.kept == [] and len(calls) == 1


@pytest.mark.parametrize("text,sign", [("(3.5%)", "-3.5"), ("(3)", "-3"), ("(2023)", "2023"),
                                       ("-3", "-3"), ("3.50", "3.5")])
def test_number_signs(text, sign):
    assert verifier._numbers(f"change was {text} in the year") == {sign}


def test_bad_direction_config_names_key(tmp_path):
    base = ("verifier:\n  overlap_low: 0.1\n  overlap_high: 0.5\n  denylist: [buy]\n"
            "  rating_words: [hold]\n  min_content_tokens: 4\n")
    p = tmp_path / "s.yaml"
    p.write_text(base + "  direction_words: {up: [rose]}\nconfidence: {}\n")
    with pytest.raises(ValueError, match="negation_words"):
        verifier.load_config(p)
    p.write_text(base + "  negation_words: ['not']\n  direction_words: {up: [rose]}\nconfidence: {}\n")
    with pytest.raises(ValueError, match="direction_words"):
        verifier.load_config(p)
    p.write_text(base + "  negation_words: ['not']\n  direction_words: {up: [rose], down: [fell]}\n"
                 "confidence: {}\n")
    with pytest.raises(ValueError, match="direction_words"):  # 'flat' group is required
        verifier.load_config(p)
    p.write_text(base + "  negation_words: ['not', 'the']\n"
                 "  direction_words: {up: [rose], down: [fell], flat: [flat]}\nconfidence: {}\n")
    with pytest.raises(ValueError, match="negation_words"):  # stopword would never match
        verifier.load_config(p)


# --- fix round 4: fast path fails closed at chunk level ---

@pytest.mark.parametrize("chunk_text,claim_text", [
    # over-splitting at unlisted abbreviations used to hide the negation in another fragment
    ("The board did not declare a dividend of Re. 1 per share for the year ended March 2024 "
     "according to the annual report.",
     "The board declared a dividend of Re 1 per share for the year ended March 2024"),
    ("The company had not repaid the term loan as of Dec. 31 2024 to the consortium lenders.",
     "The company repaid the term loan as of Dec 31 2024 to the consortium lenders"),
    ("Exports were not made to the U.S. Pharma formulations were sold only in domestic markets.",
     "Exports were made to the U.S. Pharma formulations were sold only in domestic markets"),
    # negation in a different sentence of the same chunk
    ("The board approved the merger with ABC Ltd in March 2024. The board did not approve the "
     "merger with XYZ Ltd.",
     "The board approved the merger with XYZ Ltd in March 2024"),
    # verbatim sentence followed by a qualifier
    ("The board approved the merger with ABC Ltd in March 2024. This statement was later "
     "withdrawn as incorrect.",
     "The board approved the merger with ABC Ltd in March 2024"),
    ("The board approved the merger with ABC Ltd in March 2024. It was not audited.",
     "The board approved the merger with ABC Ltd in March 2024"),
])
def test_round4_probes_go_to_model(session, inst, monkeypatch, chunk_text, claim_text):
    calls = _spy(monkeypatch)
    ch = make_chunk(session, inst, chunk_text)
    c = claim(claim_text, [ch.id])
    res = verify([c], session, instrument_id=inst.id)
    assert res.kept == [] and len(calls) == 1
    assert "negation or direction word present" in calls[0] and "in cited chunk:" in calls[0]


def test_round4_probe_overlap_would_otherwise_fast_keep(session, inst, monkeypatch):
    # The Re. probe overlaps fully: only the chunk-level flag gate stops the fast path.
    calls = _spy(monkeypatch)
    ch = make_chunk(session, inst, "The board did not declare a dividend of Re. 1 per share for the "
                    "year ended March 2024 according to the annual report.")
    c = claim("The board declared a dividend of Re 1 per share for the year ended March 2024", [ch.id])
    assert verifier.overlap(c, session) == 1.0
    verify([c], session, instrument_id=inst.id)
    assert len(calls) == 1 and "in cited chunk: not" in calls[0]


@pytest.mark.parametrize("claim_word,chunk_word", [
    ("unchanged", "flat"), ("flat", "unchanged"), ("reduced", "raised"), ("halved", "doubled"),
    ("beat", "missed"), ("outpaced", "lagged"), ("stagnant", "rebounded"), ("cut", "hike"),
    ("expansion", "contraction"), ("improvement", "reduction"),
])
def test_vocabulary_gap_pairs_go_to_model(session, inst, monkeypatch, claim_word, chunk_word):
    calls = _spy(monkeypatch)
    ch = make_chunk(session, inst, f"Segment margin {chunk_word} for the reporting quarter at Rs 120 crore")
    c = claim(f"Segment margin {claim_word} for the reporting quarter at Rs 120 crore", [ch.id])
    res = verify([c], session, instrument_id=inst.id)
    assert res.kept == [] and len(calls) == 1 and "negation or direction word present" in calls[0]
    flagged = calls[0].split("present:")[1].split("(")[0]
    assert verifier._words(claim_word) <= set(w.strip() for w in flagged.split(","))
    assert verifier._words(chunk_word) <= set(w.strip() for w in flagged.split(","))


@pytest.mark.parametrize("chunk_text", [
    "Revenue from operations reached Rs 120 crore. Nothing material was reported by auditors.",
    "Revenue from operations reached Rs 120 crore. The unit was unable to ship product.",
    "Revenue from operations reached Rs 120 crore. Disclosures lack segment detail.",
])
def test_new_negation_words_in_chunk_go_to_model(session, inst, monkeypatch, chunk_text):
    calls = _spy(monkeypatch)
    ch = make_chunk(session, inst, chunk_text)
    res = verify([claim("Revenue from operations reached Rs 120 crore", [ch.id])],
                 session, instrument_id=inst.id)
    assert res.kept == [] and len(calls) == 1 and "in cited chunk:" in calls[0]


def test_nominal_and_verbal_forms_both_flagged():
    flags = verifier._flag_words(verifier.load_config()["verifier"])
    for w in ("expansion", "expanded", "expansions", "contraction", "contracted", "reduction",
              "reduced", "improvement", "improved", "increase", "increased", "decline", "declined"):
        assert verifier._words(w) <= flags, w


def test_plain_chunk_without_flag_words_zero_model_calls(session, inst, monkeypatch):
    calls = _spy(monkeypatch)
    ch = make_chunk(session, inst, "The board approved the merger with ABC Ltd in March 2024. "
                    "The scheme covers the Pune and Chennai plants of the company.")
    c = claim("The board approved the merger with ABC Ltd in March 2024", [ch.id])
    res = verify([c], session, instrument_id=inst.id)
    assert len(res.kept) == 1 and res.dropped == []
    assert calls == []


def test_every_tied_best_chunk_is_checked(session, inst, monkeypatch):
    calls = _spy(monkeypatch)
    a = make_chunk(session, inst, "The board approved the merger with ABC Ltd in March 2024.")
    b = make_chunk(session, inst, "The board approved the merger with ABC Ltd in March 2024. "
                   "It was not audited.")
    res = verify([claim("The board approved the merger with ABC Ltd in March 2024", [a.id, b.id])],
                 session, instrument_id=inst.id)
    assert res.kept == [] and len(calls) == 1 and "in cited chunk: not" in calls[0]


def _valid_cfg():
    import copy
    return copy.deepcopy(verifier.load_config())


@pytest.mark.parametrize("mutate,key", [
    (lambda c: c["verifier"].pop("negation_words"), "negation_words"),
    (lambda c: c["verifier"].pop("direction_words"), "direction_words"),
    (lambda c: c["verifier"]["direction_words"].pop("flat"), "direction_words"),
    (lambda c: c["verifier"].pop("min_content_tokens"), "min_content_tokens"),
    (lambda c: c.pop("verifier"), "verifier"),
])
def test_bad_passed_config_raises(session, inst, no_model, mutate, key):
    cfg = _valid_cfg()
    mutate(cfg)
    ch = make_chunk(session, inst, TEXT)
    with pytest.raises(ValueError, match=key):
        verify([claim(ids=[ch.id])], session, instrument_id=inst.id, config=cfg)
    with pytest.raises(ValueError, match=key):
        verifier.overlap(claim(ids=[ch.id]), session, config=cfg)


def test_empty_passed_config_is_not_replaced_by_default(session, inst, no_model):
    with pytest.raises(ValueError):
        verify([], session, instrument_id=inst.id, config={})


def test_overlap_does_not_reload_config(session, inst, monkeypatch):
    ch = make_chunk(session, inst, TEXT)
    verifier.overlap(claim(ids=[ch.id]), session)  # warm the cache

    def boom(*a, **k):
        raise AssertionError("config reloaded")
    monkeypatch.setattr(verifier, "load_config", boom)
    assert verifier.overlap(claim(ids=[ch.id]), session) >= 0.75
    assert len(verify([claim(ids=[ch.id])], session, instrument_id=inst.id).kept) == 1


def _sector_chunk(session, sector, text, tier=1):
    from ier.db.models import Chunk, Document, Source
    s = Source(url=f"https://x.invalid/s/{abs(hash((sector, text)))}", type="report", tier=tier)
    session.add(s)
    session.flush()
    d = Document(source_id=s.id, instrument_id=None, sector=sector)
    session.add(d)
    session.flush()
    ch = Chunk(document_id=d.id, source_id=s.id, ordinal=0, text=text)
    session.add(ch)
    session.flush()
    return ch


def test_scope_must_be_exactly_one(session, inst):
    with pytest.raises(TypeError):
        verify([], session, instrument_id=inst.id, sector="IT")
    with pytest.raises(TypeError):
        verify([], session, instrument_id=None)


def test_instrument_scope_rejects_sector_document(session, inst, no_model):
    ch = _sector_chunk(session, "IT", TEXT)
    res = verify([claim(ids=[ch.id])], session, instrument_id=inst.id)
    assert res.kept == [] and "another" in reasons(res)[0]


def test_sector_scope(session, inst, no_model):
    from dataclasses import dataclass, field

    @dataclass(frozen=True)
    class Item:
        kind: str
        text: str
        severity: int
        evidence_ids: list = field(default_factory=list)

    ok = _sector_chunk(session, "IT", TEXT)
    other = _sector_chunk(session, "Banks", TEXT)
    t3 = _sector_chunk(session, "IT", TEXT, tier=3)
    company = make_chunk(session, inst, TEXT)
    items = [Item("driver", GOOD, 3, [ok.id]), Item("driver", GOOD, 3, [other.id]),
             Item("risk", GOOD, 3, [t3.id]), Item("risk", GOOD, 3, [company.id]),
             Item("benefit", GOOD, 3, [ok.id])]
    res = verify(items, session, sector="IT")
    assert res.kept == [items[0]]
    r = reasons(res)
    assert "another sector" in r[0] and "tier" in r[1] and "another sector" in r[2]
    assert "invalid kind" in r[3]
