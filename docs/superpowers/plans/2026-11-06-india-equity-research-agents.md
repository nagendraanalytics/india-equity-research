# India Equity Research Agents Implementation Plan (Phases 1-2, IT sector slice)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the foundation and a working autonomous end-to-end slice (universe filter, ingestion, fundamentals analysis, scoring, verification, scorecards, signals, parallel agent runtime, live Agent Console, analyst view and signals dashboard) for the Information Technology macro sector of NSE/BSE equities.

**Architecture:** A Python/FastAPI service with Postgres + pgvector. Workers pull jobs from a Postgres-backed queue and run sector, industry and company agents in parallel (LangGraph inside the company agent); every agent writes events that stream to the dashboard over server-sent events. Scores and signals come from versioned rule config, and a verifier drops unsourced claims. All LLM calls go through a role-to-model layer that calls open-source models behind an OpenAI-compatible endpoint (Ollama or vLLM).

**Tech Stack:** Python 3.11, FastAPI, SQLAlchemy + Alembic, PostgreSQL with pgvector, LangGraph, pytest, OpenAI-compatible client (openai SDK) against Ollama or vLLM, sentence-transformers (open embeddings), Next.js (minimal dashboard).

**Spec:** `docs/superpowers/specs/2026-11-06-india-equity-research-agents-design.md`

## Global Constraints

- Human decides and places every trade; the system never places orders.
- Ratings are research signals from rules (labels: `accumulate`, `hold`, `reduce`, `avoid`, `insufficient evidence`), never from the model, and always labelled as research signals with confidence and freshness.
- Agents run autonomously and in parallel from a job queue; every agent writes lifecycle events (`queued`, `started`, `step`, `output`, `failed`, `completed`) that the live console streams.
- Triggers are change-driven; hard compute caps halt all workers.
- Open-source models only, self-hosted behind an OpenAI-compatible endpoint, behind a role-to-model abstraction (roles: `extract`, `synthesise`, `reason`). Initial defaults in config: `extract` and `synthesise` use `qwen2.5:14b-instruct`, `reason` uses `qwen2.5:32b-instruct`; changeable by config.
- Embeddings run locally on an open model.
- Every claim in a scorecard must link to a source; unsourced claims are dropped.
- Scores (long-term, short-term) are 0-100 and computed by deterministic rules, never by the model.
- Benefit/risk severity is an integer 1-5; confidence is `low`, `medium` or `high`.
- Sub-scores: quality, growth, valuation, balance sheet, governance, sector tailwind, momentum.
- Taxonomy: 12 macro sectors, 22 sectors, 59 industries, 197 basic industries.
- Source tiers: 1 official, 2 reputable press/ratings, 3 social. Tier 3 is never evidence for a claim.
- Daily and per-run compute caps (tokens and model-seconds) are enforced and runs stop when exceeded.
- Free or official data sources only. Respect site terms; prefer official files over scraping.

## Review Focus

- Dual-listed company appears on both NSE and BSE: expect one instrument keyed by ISIN, not two.
- Suspended or surveillance-listed stock: expect it excluded from the universe, never scored.
- Company with no filings or a stale filing: expect a scorecard marked low confidence with the staleness shown, not a guessed value.
- Model returns invalid or non-schema JSON: expect bounded retries, then the field is marked failed, not silently empty.
- Run exceeds the budget cap mid-way: expect all workers to halt, unfinished jobs to stay queued, and the console to show which agents did not finish.
- Dashboard client disconnects and reconnects mid-run: expect full current tree state first, then new events, with no duplicated or missing nodes.

## File Structure

```
india-equity-research/
  pyproject.toml
  alembic/                      migrations
  src/ier/
    db/models.py                tables from spec section 7
    taxonomy/loader.py          NSE classification -> instruments
    universe/filter.py          universe rules
    tools/base.py               fetch, cache, hash, provenance
    tools/exchange_filings.py   filings connector
    llm/router.py               role -> model, compute caps
    knowledge/chunking.py       chunk + embed + store
    knowledge/retrieval.py      hybrid retrieval, metadata filter
    rules/engine.py             rule loading and evaluation
    rules/config/it.yaml        IT scoring and red-flag rules
    agents/fundamentals.py      fact extraction
    agents/synthesis.py         benefits/risks drafting
    agents/verifier.py          claim-source check
    agents/sector.py            sector and industry agents
    runtime/queue.py            job queue (claim, retry)
    runtime/triggers.py         change-driven job creation
    runtime/worker.py           parallel workers, concurrency limits
    runtime/events.py           agent registry, event log, status tree
    graph/company.py            company agent graph (LangGraph)
    rules/config/signals.yaml   signal rating rules
    signals/compute.py          rule-based signals
    api/main.py                 FastAPI endpoints + SSE stream
  tests/                        mirrors src
  dashboard/                    Next.js minimal views
```

---

### Task 1: Project scaffold and schema

**Files:**
- Create: `pyproject.toml`, `src/ier/db/models.py`, `alembic/`, `tests/db/test_models.py`

**Interfaces:**
- Produces: SQLAlchemy models `Instrument`, `Source`, `Document`, `Chunk`, `Fact`, `Analysis`, `Scorecard`, `EvidenceLink`, `RunLog`, `Agent`, `Job`, `AgentEvent`, `Signal` with the columns in spec section 7; `get_session() -> Session`.

- [ ] **Step 1:** `git init`; create the package skeleton and a Postgres test fixture with pgvector enabled.
- [ ] **Step 2:** Write failing tests: `test_instrument_unique_by_isin` (inserting two rows with the same ISIN raises IntegrityError); `test_scorecard_versioned_by_run_date` (two scorecards for one instrument with different run dates both persist); `test_evidence_link_requires_source` (EvidenceLink with no fact or chunk raises).
- [ ] **Step 3:** Run `pytest tests/db -v`. Expected: FAIL (models missing).
- [ ] **Step 4:** Implement models and the initial Alembic migration.
- [ ] **Step 5:** Run `pytest tests/db -v`. Expected: PASS.
- [ ] **Step 6:** Commit `feat: schema and scaffold`.

### Task 2: Taxonomy loader

**Files:**
- Create: `src/ier/taxonomy/loader.py`, `tests/taxonomy/test_loader.py`, `tests/fixtures/classification_sample.csv`

**Interfaces:**
- Consumes: `Instrument` model.
- Produces: `load_classification(path: Path) -> int` (rows upserted); `Instrument` gets `macro_sector`, `sector`, `industry`, `basic_industry`.

- [ ] **Step 1:** Write failing tests: `test_loads_four_levels` (sample row yields all four tags); `test_dual_listed_merged_by_isin` (NSE and BSE rows with one ISIN produce one instrument with both exchange codes); `test_unclassified_company_flagged` (missing basic industry sets `classification_status == "missing"`).
- [ ] **Step 2:** Run `pytest tests/taxonomy -v`. Expected: FAIL.
- [ ] **Step 3:** Implement `load_classification` in `src/ier/taxonomy/loader.py`. Upsert by ISIN.
- [ ] **Step 4:** Run tests. Expected: PASS.
- [ ] **Step 5:** Commit `feat: taxonomy loader`.

### Task 3: Universe filter

**Files:**
- Create: `src/ier/universe/filter.py`, `tests/universe/test_filter.py`

**Interfaces:**
- Consumes: `Instrument` with tags.
- Produces: `build_universe(session, macro_sector: str | None, min_listing_days: int = 365) -> list[Instrument]`.

- [ ] **Step 1:** Write failing tests: `test_excludes_suspended`; `test_excludes_surveillance_listed`; `test_excludes_recent_listing` (listed under `min_listing_days`); `test_filters_by_macro_sector` ("Information Technology" returns only IT companies); `test_excludes_illiquid` (average daily traded value below the configured threshold).
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement `build_universe`. Thresholds come from `config/universe.yaml`.
- [ ] **Step 4:** Run. Expected: PASS.
- [ ] **Step 5:** Commit `feat: universe filter`.

### Task 4: Tool layer base with provenance

**Files:**
- Create: `src/ier/tools/base.py`, `tests/tools/test_base.py`

**Interfaces:**
- Consumes: `Source` model.
- Produces: `fetch(url: str, tier: int, session) -> FetchResult` where `FetchResult(source_id: int, content: bytes, changed: bool)`; honours rate limit, retries with backoff, stores content hash.

- [ ] **Step 1:** Write failing tests (HTTP mocked): `test_records_source_with_hash_and_tier`; `test_unchanged_content_returns_changed_false`; `test_retries_then_marks_stale` (three failures set `Source.status == "stale"`, no exception to caller); `test_rate_limit_respected`.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement `fetch` with an on-disk cache keyed by URL.
- [ ] **Step 4:** Run. Expected: PASS.
- [ ] **Step 5:** Commit `feat: provenance-tracked fetch`.

### Task 5: LLM router with compute caps

**Files:**
- Create: `src/ier/llm/router.py`, `config/models.yaml`, `tests/llm/test_router.py`

**Interfaces:**
- Produces: `complete(role: Literal["extract","synthesise","reason"], prompt: str, schema: type[BaseModel], run_id: str) -> BaseModel`; raises `BudgetExceeded` when the run or daily compute cap (tokens or model-seconds) is hit. `config/models.yaml` holds `base_url`, per-role model names (defaults in Global Constraints) and the caps. The client is the `openai` SDK pointed at `base_url`.

- [ ] **Step 1:** Write failing tests (client mocked): `test_role_maps_to_configured_model_and_base_url`; `test_invalid_json_retries_twice_then_raises_schema_error`; `test_cap_exceeded_raises_before_call`; `test_usage_recorded_in_run_log`.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement `complete` with schema validation, bounded retry, and token and model-second accounting against caps from config.
- [ ] **Step 4:** Run. Expected: PASS.
- [ ] **Step 5:** Commit `feat: llm router and compute caps`.

### Task 6: Knowledge store (chunk, embed, retrieve)

**Files:**
- Create: `src/ier/knowledge/chunking.py`, `src/ier/knowledge/retrieval.py`, `tests/knowledge/test_retrieval.py`

**Interfaces:**
- Consumes: `Source`, `Document`, `Chunk`.
- Produces: `ingest_document(session, source_id: int, text: str, meta: dict) -> int` (chunks stored); `retrieve(session, query: str, instrument_id: int, k: int = 8) -> list[Chunk]` (metadata-filtered hybrid search).

- [ ] **Step 1:** Write failing tests: `test_chunks_carry_metadata`; `test_retrieval_only_returns_requested_company` (two companies ingested, query for one returns none of the other's chunks); `test_hybrid_matches_exact_term` (keyword hit ranks first for an exact figure string).
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement with a local sentence-transformers model and pgvector plus Postgres full-text search.
- [ ] **Step 4:** Run. Expected: PASS.
- [ ] **Step 5:** Commit `feat: knowledge store`.

### Task 7: Rules engine and IT rules

**Files:**
- Create: `src/ier/rules/engine.py`, `src/ier/rules/config/it.yaml`, `tests/rules/test_engine.py`

**Interfaces:**
- Consumes: `Fact` rows.
- Produces: `evaluate(instrument_id: int, rules_path: Path, session) -> RuleResult` with `sub_scores: dict[str, float]` (the seven sub-scores), `long_term: float`, `short_term: float`, `red_flags: list[str]`, `rules_version: str`.

- [ ] **Step 1:** Write failing tests: `test_scores_within_0_100`; `test_same_facts_same_scores` (determinism); `test_high_promoter_pledging_raises_red_flag`; `test_missing_fact_lowers_confidence_not_score_to_zero` (missing input yields `None` for that sub-score and a missing-data note); `test_rules_version_recorded`.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement the engine with a YAML rule schema (metric, comparison to industry median, weight, horizon). The IT file covers margins, revenue growth, attrition, debt/equity and pledging.
- [ ] **Step 4:** Run. Expected: PASS.
- [ ] **Step 5:** Commit `feat: rules engine`.

### Task 8: Exchange filings connector and fundamentals analyst

**Files:**
- Create: `src/ier/tools/exchange_filings.py`, `src/ier/agents/fundamentals.py`, `tests/agents/test_fundamentals.py`

**Interfaces:**
- Consumes: `fetch`, `ingest_document`, `retrieve`, `complete`.
- Produces: `list_filings(isin: str) -> list[FilingRef]`; `extract_facts(session, instrument_id: int, run_id: str) -> list[Fact]` writing `Fact(metric, value, period, source_id)`.

- [ ] **Step 1:** Write failing tests (fixtures of one results filing; LLM mocked): `test_extracts_revenue_and_margin_with_period_and_source`; `test_fact_without_source_not_saved`; `test_conflicting_values_both_kept_and_flagged`; `test_no_filings_returns_empty_and_marks_stale`.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement the connector against the official exchange filing files (verify access and terms first, per the spec's open items) and the analyst using the `extract` role with a Pydantic schema.
- [ ] **Step 4:** Run. Expected: PASS.
- [ ] **Step 5:** Commit `feat: filings and fundamentals analyst`.

### Task 9: Synthesis, verifier and scorecard

**Files:**
- Create: `src/ier/agents/synthesis.py`, `src/ier/agents/verifier.py`, `tests/agents/test_synthesis.py`, `tests/agents/test_verifier.py`

**Interfaces:**
- Consumes: `RuleResult`, `Fact`, `retrieve`, `complete`.
- Produces: `draft_claims(session, instrument_id, rule_result, run_id) -> list[Claim]` (`Claim(kind: "benefit"|"risk", text, severity: int 1-5, horizon: "short"|"long", evidence_ids: list[int])`); `verify(claims, session) -> VerifiedClaims` (kept claims plus dropped list with reasons); `build_scorecard(...) -> Scorecard`.

- [ ] **Step 1:** Write failing tests: `test_claim_without_evidence_dropped`; `test_claim_not_supported_by_cited_chunk_dropped`; `test_tier3_source_cannot_support_claim`; `test_scorecard_scores_come_from_rule_result` (model output cannot alter the scores); `test_confidence_low_when_evidence_sparse`; `test_claims_contain_no_order_or_instruction_wording` (no order verbs or instructions addressed to the reader).
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement. The verifier checks each cited chunk with a deterministic overlap check first, then a `extract`-role model check for borderline cases.
- [ ] **Step 4:** Run. Expected: PASS.
- [ ] **Step 5:** Commit `feat: synthesis and verifier`.

### Task 10: Sector and industry agents

**Files:**
- Create: `src/ier/agents/sector.py`, `tests/agents/test_sector.py`

**Interfaces:**
- Consumes: `retrieve`, `complete` (role `reason`), `Scorecard` rows of member companies.
- Produces: `analyse_sector(session, sector: str, run_id) -> SectorView` (drivers, risks, tailwind sub-score input); `rank_industry(session, industry: str) -> list[tuple[int, float]]` (instrument id, long-term score, peer-only).

- [ ] **Step 1:** Write failing tests: `test_ranking_compares_only_same_industry_peers`; `test_sector_view_cites_sources`; `test_empty_sector_returns_no_ranking_without_error`.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement. The sector view feeds the "sector tailwind" fact used by the rules engine.
- [ ] **Step 4:** Run. Expected: PASS.
- [ ] **Step 5:** Commit `feat: sector and industry agents`.

### Task 11: Job queue, triggers and parallel workers

**Files:**
- Create: `src/ier/runtime/queue.py`, `src/ier/runtime/triggers.py`, `src/ier/runtime/worker.py`, `tests/runtime/test_queue.py`, `tests/runtime/test_worker.py`

**Interfaces:**
- Consumes: `Job`, `Agent`, `fetch` (changed flag), `complete` (`BudgetExceeded`).
- Produces: `enqueue(session, agent_id: int, trigger: str, scheduled_at: datetime | None = None) -> Job`; `claim_next(session, tier: str) -> Job | None` (row-locked, one claimant only); `on_new_source(session, source_id: int) -> list[Job]` (jobs only for agents whose inputs changed); `run_workers(tiers: dict[str, int], stop: Event) -> None` (concurrency per tier).

- [ ] **Step 1:** Write failing tests: `test_job_claimed_by_one_worker_only` (two concurrent claimants, one gets the job); `test_unchanged_source_creates_no_job`; `test_new_filing_creates_job_for_that_company_agent_only`; `test_concurrency_limit_per_tier_respected`; `test_budget_exceeded_halts_all_workers_and_leaves_jobs_queued`; `test_failed_job_retries_then_marks_failed` (attempts capped at 3).
- [ ] **Step 2:** Run `pytest tests/runtime -v`. Expected: FAIL.
- [ ] **Step 3:** Implement with Postgres `SELECT ... FOR UPDATE SKIP LOCKED`; triggers cover new source, price/volume event threshold, schedule, and manual request.
- [ ] **Step 4:** Run. Expected: PASS.
- [ ] **Step 5:** Commit `feat: job queue and parallel workers`.

### Task 12: Agent registry, event log and status tree

**Files:**
- Create: `src/ier/runtime/events.py`, `tests/runtime/test_events.py`

**Interfaces:**
- Consumes: `Agent`, `AgentEvent`, taxonomy tags.
- Produces: `seed_agents(session, macro_sector: str) -> int` (master, macro, sectors, industries, companies, workers for the slice); `emit(session, agent_id: int, kind: str, payload: dict, cost: float = 0.0) -> AgentEvent`; `tree(session, root_id: int | None = None) -> AgentNode` (`AgentNode(id, tier, name, status, current_task, last_run, cost_today, children)`).

- [ ] **Step 1:** Write failing tests: `test_tree_built_from_taxonomy` (master → macro → sector → industry → company → workers, parent links correct); `test_parent_status_running_if_any_child_running`; `test_failed_child_does_not_fail_siblings`; `test_events_are_append_only` (update/delete raises); `test_cost_rolls_up_to_parents`.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement. `emit` is the only write path to `agent_events`; kinds limited to `queued`, `started`, `step`, `output`, `failed`, `completed`.
- [ ] **Step 4:** Run. Expected: PASS.
- [ ] **Step 5:** Commit `feat: agent registry and event log`.

### Task 13: Company agent graph and execution order

**Files:**
- Create: `src/ier/graph/company.py`, `tests/graph/test_company.py`

**Interfaces:**
- Consumes: Tasks 3, 7, 8, 9, 10, 12.
- Produces: `run_company_agent(session, agent_id: int, run_id: str) -> RunResult` (`status`, `scorecard_id | None`); registered as the handler workers call for tier `company`. Company jobs depend on the industry and sector jobs for the same run (`depends_on` on `Job`).

- [ ] **Step 1:** Write failing tests (agents stubbed): `test_company_job_waits_for_sector_and_industry_jobs`; `test_each_worker_emits_started_step_output_completed`; `test_unchanged_inputs_skipped_with_event`; `test_worker_failure_emits_failed_and_run_continues_for_other_workers`; `test_run_log_records_inputs_and_model_version`.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement the LangGraph graph (fundamentals → rules → synthesis → verifier → scorecard → signal) emitting events at each node.
- [ ] **Step 4:** Run. Expected: PASS.
- [ ] **Step 5:** Commit `feat: company agent graph`.

### Task 14: Signal rules

**Files:**
- Create: `src/ier/signals/compute.py`, `src/ier/rules/config/signals.yaml`, `tests/signals/test_compute.py`

**Interfaces:**
- Consumes: `RuleResult`, `Scorecard`.
- Produces: `compute_signal(scorecard: Scorecard, rule_result: RuleResult, rules_path: Path) -> Signal` with `rating` in `{"accumulate","hold","reduce","avoid","insufficient evidence"}`, `rules_version`, `reasons: list[str]`.

- [ ] **Step 1:** Write failing tests: `test_red_flag_caps_rating_at_hold_or_below`; `test_low_confidence_gives_insufficient_evidence_and_no_other_rating`; `test_same_inputs_same_rating`; `test_rating_always_from_allowed_set`; `test_signal_has_no_order_fields` (no quantity, price or side).
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement threshold mapping from `signals.yaml` (long-term and short-term score bands, confidence gate, red-flag cap).
- [ ] **Step 4:** Run. Expected: PASS.
- [ ] **Step 5:** Commit `feat: signal rules`.

### Task 15: API with live event stream

**Files:**
- Create: `src/ier/api/main.py`, `tests/api/test_api.py`

**Interfaces:**
- Consumes: `tree`, `AgentEvent`, `Scorecard`, `Signal`, `EvidenceLink`.
- Produces: `GET /agents/tree`, `GET /agents/{id}` (detail, cost, last output), `GET /agents/{id}/events`, `GET /stream` (server-sent events, supports `Last-Event-ID`), `POST /agents/{id}/rerun`, `GET /analysts/{agent_id}` (findings, cited evidence, run history, rule inputs), `GET /sectors`, `GET /stocks?sector=&industry=&sort=`, `GET /stocks/{isin}`, `GET /signals?sector=&rating=`.

- [ ] **Step 1:** Write failing tests: `test_tree_endpoint_returns_nested_status`; `test_stream_emits_new_event_to_connected_client`; `test_reconnect_with_last_event_id_gets_no_duplicates_or_gaps`; `test_agent_detail_includes_cost_and_last_output`; `test_rerun_enqueues_job_and_emits_queued`; `test_analyst_endpoint_lists_cited_evidence`; `test_signals_include_confidence_freshness_and_label`; `test_stale_data_age_exposed`; `test_stocks_sortable_by_long_term_score`.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement endpoints; the stream reads new `agent_events` rows by id; the tree endpoint supplies full state for reconnecting clients.
- [ ] **Step 4:** Run `pytest tests/api -v`. Expected: PASS.
- [ ] **Step 5:** Commit `feat: api and event stream`.

### Task 16: Dashboard views

**Files:**
- Create: `dashboard/` views: Agent Console, Analyst view, Signals view, Sector table, Stock page; component tests with fixtures.

**Interfaces:**
- Consumes: Task 15 endpoints and `/stream`.

- [ ] **Step 1:** Write failing component tests: `console renders tree with status per node`; `clicking a node drills down one level and shows its children`; `new stream event updates node status without reload`; `on reconnect console reloads tree before applying events`; `signals view shows label, confidence and freshness`; `analyst view lists cited evidence links`.
- [ ] **Step 2:** Run the dashboard test command. Expected: FAIL.
- [ ] **Step 3:** Implement the views. The console shows queue depth, compute used today and failures with a re-run button; every screen shows data freshness.
- [ ] **Step 4:** Run tests, then start the dashboard against seeded data and check each view loads. Expected: PASS and views render.
- [ ] **Step 5:** Commit `feat: dashboard views`.

### Task 17: IT slice acceptance run

**Files:**
- Create: `tests/golden/it_golden.yaml`, `tests/golden/test_golden.py`

- [ ] **Step 1:** You hand-check scorecards for 5 IT companies and record expected sub-score ranges, red flags and signal ratings in `it_golden.yaml`.
- [ ] **Step 2:** Write `test_golden_scores_within_ranges`, `test_every_claim_has_source`, and `test_full_run_leaves_no_agent_in_running_state`.
- [ ] **Step 3:** Start the workers against the IT slice with real data and the real model; watch the console; then run `pytest tests/golden -v`. Expected: PASS, or a list of rule and prompt fixes to make.
- [ ] **Step 4:** Commit `test: IT golden set`.

---

## Self-Review Notes

- Spec coverage (revision 2): schema and agents/jobs/events/signals (Task 1); autonomous runtime (Tasks 11-13); signals (Task 14); live console, analyst view, signals view (Tasks 15-16); acceptance (Task 17). Macro leads, technicals, news/governance agents, portfolio tracking and order execution are deferred.
- Types: `RuleResult`, `Claim`, `Scorecard`, `Signal`, `AgentNode`, `RunResult` are defined once and reused with the same names.
- Open spec items affect Task 8 (data source access and terms) and Task 5 (compute cap values and `base_url` in `config/models.yaml`).
