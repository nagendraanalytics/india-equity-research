# India Equity Research Agents: Design Spec

Date: 2026-11-06
Status: Revision 3 (approved by owner; model constraint changed to open-source only).

## 1. Purpose and scope

A personal research system for NSE/BSE listed equities. Autonomous agents run continuously in parallel in the backend, researching many stocks and sectors, and produce a benefit/risk scorecard per stock and per sector. A live dashboard shows every agent. A human makes every decision and places every trade.

In scope:
- Long-term (1+ year) fundamental view plus a short-term (weeks to months) timing layer.
- Ranked comparison across stocks, industries and sectors, with cited evidence.
- Autonomous, event-driven and scheduled agent runs, with a live Agent Console that drills down from the master orchestrator to each analyst worker.
- Per-analyst dashboards showing findings, evidence and reasoning.
- A Signals view: a rule-based rating per stock (accumulate, hold, reduce, avoid, or insufficient evidence), labelled as a research signal, with the benefits, risks and evidence behind it.

Out of scope:
- Order placement or any broker execution (a possible separate later phase, which would bring SEBI algo rules into play).
- Presenting signals as advice or as instructions to trade. The Signals view is a research output for the owner.
- Use by anyone other than the owner. Offering it to others may need SEBI research analyst or adviser registration and is a separate decision.

## 2. Constraints

- Open-source models only, self-hosted behind an OpenAI-compatible endpoint (vLLM or Ollama, on a local machine or a rented GPU). No per-token paid APIs.
- Model access goes through one abstraction layer that maps roles to model names and an endpoint URL, so models can be swapped by config without changing agents.
- With no per-token cost, the limits are compute limits: hard caps on tokens and model-seconds per run and per day, incremental runs and caching.
- Free or official data sources first. A paid feed is a later option.
- Every claim must link to a source. Unsourced claims are dropped.
- Scores are computed by deterministic rules, not by the model.

## 3. Market structure the system relies on

Industry classification (shared by NSE and BSE): 12 macro-economic sectors, 22 sectors, 59 industries, 197 basic industries. Each company sits in exactly one basic industry, so it rolls up to exactly one branch. Conglomerates go under "Diversified".

Other classes used as tags: size class (large, mid, small by AMFI ranking), listing segment (main board or SME), BSE group / NSE series and surveillance lists, index membership, ownership class (PSU, private, MNC, promoter and FPI/DII holding), instrument type (equity only at first).

Universe: about 5,500 unique equity securities. The universe filter reduces this to a screened set (target a few hundred) by excluding suspended, illiquid, surveillance-listed and very new listings.

Note: the size, segment, group and ownership class definitions must be verified against current exchange and AMFI rules at build time.

## 4. Architecture

Data flows one way: ingestion, knowledge layer, agents, rules, verifier, storage, dashboard.

### 4.1 Agent tiers

An agent is a reusable role. Per-entity instances run the same role with that entity's data and memory.

1. Chief orchestrator (1): schedules runs, enforces cost limits, assembles output.
2. Macro-sector leads (12 instances): policy, demand cycle, global factors.
3. Sector agents (22 instances): sector drivers, risks, peer ranking.
4. Industry agents (59 instances): peer benchmarking within true peers.
5. Company agents (1 instance per company in the universe), each with four workers: fundamentals, governance and news, technicals, synthesis plus verification.

Context flows down (sector and industry views inform each company). Evidence flows up (company scorecards roll up into industry, sector and macro summaries).

Scale: 94 higher-level runs per cycle, plus N companies x 4 workers. For 300 companies this is about 1,300 runs per cycle. Each company agent keeps persistent memory (past scorecards, filings read, open questions, last-run date) and only processes what changed.

Rollout: start with company, industry and sector agents on one sector. Add macro leads later.

### 4.2 Orchestration (LangGraph)

- Fan-out and fan-in graph. Sector and industry agents run first, then company agents in parallel, then synthesis and verification.
- Schedules: daily for technicals and news, quarterly for fundamentals, plus on-demand runs for watchlist changes.
- Cost guard: token and call caps per run. Unchanged inputs are skipped using content hashes.

### 4.2a Autonomous runtime and live agent console

- Agents run as workers pulling jobs from a queue (Postgres-backed in phase 1). A job is created by a trigger: a new filing or announcement, a significant price or volume event, a scheduled refresh, or a manual request. Workers run in parallel with a concurrency limit per agent tier.
- Triggers are change-driven. Unchanged inputs create no job, which keeps compute use bounded. Daily and per-run compute caps still stop all workers.
- Every agent instance writes lifecycle events to an event log: `queued`, `started`, `step` (tool call or model call, with cost), `output`, `failed`, `completed`. Each event carries agent id, tier, parent agent id, instrument or sector id, and run id.
- The agent hierarchy is a tree: master orchestrator → macro sectors → sectors → industries → companies → workers. A node's status is derived from its own events and its children's (for example, running if any child is running).
- The API streams events to the dashboard (server-sent events in phase 1). A reconnecting client first loads the current tree state, then resumes the stream.
- Failures are isolated to the failed node and shown in the console. Retries are bounded, and a manual re-run is available from the console.

### 4.3 Models

- All LLM roles use open-source models served locally or on a rented GPU through an OpenAI-compatible API. Initial defaults (benchmark on sample Indian filings and change by config): `extract` and `synthesise` use a mid-size instruct model (about 14B), `reason` uses the largest model the hardware supports (about 32B). Candidate families: Qwen, Llama, Mistral, DeepSeek, Gemma.
- Embeddings: an open embedding model run locally.
- Model abstraction layer: agents request a role (extract, synthesise, reason), and config maps each role to a model name and endpoint. A different open model, or later a hosted model, replaces any role by config.
- Compute controls: numbers and scores computed in code, structured JSON outputs validated by code with retry, small focused prompts, incremental runs using content hashes, response caching, concurrency limits per tier, and daily and per-run token and model-second caps.
- Open models are weaker at long reasoning than frontier models, so keep the strict verifier, rule-based scores and signals, and hand-check early narratives.

## 5. Knowledge and rules layer

### 5.1 Rules engine (deterministic)
- Scoring rules: formulas from facts to sub-scores (quality, growth, valuation, balance sheet, governance, sector tailwind, momentum), for example debt/equity versus industry median.
- Red-flag rules: high promoter pledging, auditor qualification, surveillance listing, repeated filing delays, regulatory action.
- Policy rules: no order placement, no claim without a Tier 1 or Tier 2 source, and signals are research labels only.
- Signal rules (config file): map long-term score, short-term score, red flags and confidence to a rating. Any red flag caps the rating at "hold" or below. Low confidence yields "insufficient evidence" and no rating. Ratings are computed by rules, never by the model.
- Industry-specific rules (banks: NPA and capital ratios; IT: margins and attrition; and so on).
- Rules are versioned config files, so old runs can be re-scored after tuning.

### 5.2 Vector store (pgvector first)
- Document chunks with metadata: company, sector, date, source tier, document type.
- Hybrid retrieval: keyword plus vector, filtered by metadata, so a company agent sees only its own documents.
- Company memory collection and a curated knowledge base (sector notes, rule explanations, glossary).
- Separate collections per type.

### 5.3 Classification layer
- Taxonomy tags (industry hierarchy plus size, segment, risk group, ownership).
- Document classification: type (results, rating action, regulatory order, management change), sentiment, event severity.
- Claim classification: benefit or risk, short-term or long-term.
- Entity linking via name, ticker and ISIN.

## 6. External tool layer

Each source is wrapped as a tool (MCP-style) with rate limits, caching, retries and provenance logging. Agents call tools and never scrape directly.

| Agent | Sources (to verify access and terms at build time) |
|---|---|
| Universe filter | NSE and BSE listing and classification files, NSE bhavcopy, surveillance and suspension lists |
| Fundamentals | Exchange filings (results, annual reports, shareholding, XBRL), filed earnings call transcripts, credit rating reports |
| Governance and news | Exchange announcements, SEBI orders, news via RSS or open aggregators |
| Technicals | Official end-of-day prices; broker APIs for intraday; unofficial libraries as fallback only |
| Sector and industry | RBI, government statistics, data.gov.in, industry bodies, FPI/DII flow data |
| Sentiment | Reddit API, StockTwits, X (paid and restrictive). Telegram and WhatsApp tips excluded due to manipulation risk |

Source reliability tiers:
1. Official (exchange, SEBI, RBI): can support a scored claim.
2. Reputable press and ratings: can support a claim with a note.
3. Social and forums: sentiment signal only, never evidence. Spikes raise an "unverified chatter" flag.

Respect each site's terms. Prefer official files and APIs over scraping.

## 7. Data model (Postgres)

- instruments (stock or sector, NSE/BSE identifiers, ISIN, taxonomy tags)
- sources (URL, type, tier, publish date, fetch date, content hash)
- documents and chunks (text, embeddings, link to source)
- facts (numeric data points with period and source)
- analyses (one row per agent run with findings)
- scorecards (synthesised output, versioned by run date)
- evidence_links (each benefit, risk or score linked to facts or chunks)
- run_log (inputs, prompts, model version, outputs for audit and reproducibility)
- agents (id, tier, parent id, scope instrument or sector, role)
- jobs (queue: trigger type, agent id, status, attempts, scheduled time)
- agent_events (append-only lifecycle events described in 4.2a, with cost and a pointer to outputs)
- signals (instrument id, rating, scorecard id, rules version, run date)

## 8. Scorecard

Per stock, one fixed shape:
- Benefits and risks: statement, category, strength or severity (1-5), horizon, linked evidence.
- Long-term score (0-100) and short-term score (0-100), built from sub-scores by rules.
- Confidence (low, medium, high) from evidence volume and agent agreement.
- Red flags shown above the score.

Sector and industry scorecards use the same shape plus a ranking of member companies.

## 9. Error handling

- Source failure: retry with backoff, mark the field stale, show its age. Never fill gaps with model guesses.
- Conflicting data: keep both values, show the conflict, lower confidence.
- Unsourced claim: dropped by the verifier.
- Partial run: publish what completed and list what is missing.
- Full audit log for every run.

## 10. Dashboard (Next.js)

- Sector heatmap (22 sectors, long and short-term scores, trend, confidence).
- Ranked stock table with filters (sector, industry, size, risk group) and sorting.
- Stock page with scorecard and click-through to cited evidence.
- Compare view for 2-5 stocks or sectors.
- Agent Console (live): the agent tree with status, current task, last run, cost and latest output per node. Click any node to drill down a level, down to each analyst worker. Shows queue depth, compute used today and failures, with a re-run button.
- Analyst view: for any analyst (for example fundamentals for one company), its findings, cited evidence, run history and rule inputs, so you can understand and challenge it.
- Signals view: ratings per stock, sector and industry with filters, the scores and red flags behind each rating, and a "why" panel linking to evidence. Every rating is labelled as a research signal and shows confidence and data freshness.
- Data freshness shown on every screen.

## 11. Testing

- Golden set of stocks with hand-checked scorecards.
- Automated checks that every claim has a source and that scores reproduce from the same facts.
- Early outputs hand-reviewed, especially the written narrative from open models.

## 12. Build phases

1. Foundation: schema (including agents, jobs, agent_events), exchange classification and listings loaded, universe filter, source tracking.
2. Fundamentals MVP: filings ingestion, fundamentals analyst, synthesis and verifier for 20-30 stocks in 3-4 sectors. First slice: one sector end to end (for example IT or Banks).
2a. Autonomous runtime and Agent Console: job queue, parallel workers, triggers, event log, streaming API, live console with drill-down, and analyst view (on the IT slice).
3. Sector, industry and risk: sector and industry agents, governance and news agents.
4. Timing and dashboard polish: technicals, scheduling, compare view, golden-set regression tests.
5. Later (optional): macro leads, portfolio tracking, alerts, backtesting of past scores, paid data feed.

## 13. Risks

- Free data may be inconsistent or delayed: mitigated by source tracking and staleness flags.
- GPU and compute limits grow with universe size: mitigated by compute caps, incremental runs, caching and a screened universe.
- Open-model errors and weaker reasoning: mitigated by rule-based scores, structured outputs and the verifier.
- Over-reliance: this is decision support, not investment advice, and past scores do not predict returns. Signals are labelled as research outputs.
- Runaway compute from continuous agents: mitigated by change-driven triggers, concurrency limits and hard compute caps that stop all workers.
- Stale or noisy live view: the console shows event timestamps, and a reconnecting client reloads full state first.
- Regulatory: SEBI is preparing AI-in-trading guidelines (not confirmed issued as of the research for this spec). Personal, human-decides use with no execution is the lowest-risk posture. Re-check before any change in scope.

## 14. Open items

- Pick the first sector for the vertical slice.
- Choose hosting (local machine or rented GPU) and set the daily and per-run compute caps.
- Benchmark candidate open models on sample Indian filings before fixing role defaults.
- Confirm access and terms for each data source.
- Verify size-class, segment, group and ownership definitions against current official rules.
