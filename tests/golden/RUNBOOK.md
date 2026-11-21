# IT slice acceptance: what only you can do

The automated golden run (`pytest tests/golden`) uses INVENTED companies, a stub model, a stub
network and a fake embedder. It proves the plumbing end to end and guards the rules as configured.
It does not prove the system works on real filings with a real model, and it does not say the
placeholder benchmarks are right. This runbook is the rest of Task 17. Every step below needs
something this workspace does not have (a model server, exchange access, your judgement).

All commands run from the repo root with the venv active (`source .venv/bin/activate`).

## Read this first: what the system does NOT do yet

1. **It is not autonomous yet.** The change-driven triggers (`on_new_source`, `on_market_event`,
   `on_schedule` in `src/ier/runtime/triggers.py`) exist and are tested, but nothing calls them.
   Runs happen only when you queue one with `python -m ier.runtime plan ...` or press re-run in
   the dashboard (`POST /agents/{id}/rerun`).
2. **The "short-term" score is not a timing signal.** It is the weighted score of attrition and
   promoter pledging only. There is no technicals, news or macro agent (deferred).
3. **Freshness shown is the scorecard run date, not the age of the data.** A run whose filings did
   not change carries the previous facts forward; the date says when it was scored, not how old
   the underlying filing is.
4. **The industry ranking reflects the previous run.** The industry job runs before its companies
   in the same run, so it ranks the scorecards from the last completed run.
5. **Install with `pip install -e .`.** `pyproject.toml` package-data covers only `ier.api`
   config, so a non-editable install misses the other YAML config files.
6. **`POST /agents/{id}/rerun` has no authentication.** The API binds to 127.0.0.1; never expose
   it beyond loopback (no reverse proxy, no 0.0.0.0 / `IER_API_HOST`, no port forwarding).

Every uncalibrated number is listed in [docs/PLACEHOLDERS.md](../../docs/PLACEHOLDERS.md).

## 0. What is automated and what is not

| Item | State |
|---|---|
| Synthetic golden set, ranges derived by hand from `it.yaml` / `signals.yaml` | done: `tests/golden/it_golden.yaml` |
| The three named tests + extras | done: `tests/golden/test_golden.py` |
| Live run against real model and data | written, skipped by default: `tests/golden/test_live.py` |
| Hand-checked scorecards for 5 REAL IT companies | OPEN, yours (section 6) |
| Calibrated benchmarks, scales, weights, bands | OPEN, yours (section 7) |
| Compute cap values | OPEN, yours (section 2) |
| Exchange filing access and terms | OPEN, yours (section 3) |

## 0b. Install the package once

```
python -m venv .venv && . .venv/bin/activate
pip install -e .   # without this, `python -m ier.runtime` and `python -m ier.api` fail with "No module named ier"
```

## 1. Postgres

```
# any Postgres 16 with the pgvector extension; example for a local dev instance on 5432
createdb -U postgres ier
export IER_DATABASE_URL=postgresql+psycopg://postgres@localhost:5432/ier
alembic upgrade head
```
(The test database is `ier_test` on port 5433 and is WIPED by the test suite. Never point
`IER_DATABASE_URL` at it for the live run; the live test refuses a `*_test` database.)

## 2. Model server and compute caps (`config/models.yaml`)

1. Run an OpenAI-compatible server (Ollama or vLLM) and set `base_url` (Ollama default
   `http://localhost:11434/v1`).
2. Pull/serve the models named under `models:` (`extract`, `synthesise`, `reason`), e.g.
   `ollama pull qwen2.5:14b-instruct` and `ollama pull qwen2.5:32b-instruct`. Change the names
   if you use other models.
3. Decide the caps under `caps:` (`per_run` and `per_day`: `max_tokens`, `max_model_seconds`).
   The shipped numbers are modest guesses (200k tokens / 30 min per run, 1M tokens / 2 h per day).
   `per_run` is the budget of ONE job: one company's analysis (or one sector job) within a run,
   not the whole sector run. Hitting it marks that job failed (no retry; the others go on).
   Hitting `per_day` stops every worker and puts the job back in the queue for the next UTC
   midnight; `workers --drain` then exits non-zero and reports it under `backoff`. Each worker
   running in parallel may overshoot a cap by one call.
4. Check the server answers: `curl <base_url>/models`.

Embeddings use `BAAI/bge-base-en-v1.5` (`config/knowledge.yaml`) via sentence-transformers; the
first run downloads it, so the machine needs access to Hugging Face once (or pre-download it).

## 3a. Local-folder filings (no web access needed)

Set `local_dir` in `src/ier/tools/config/exchange_filings.yaml` (use forward slashes or single
quotes on Windows, e.g. `local_dir: 'C:/projects/india-equity-research/data/filings'`) and put
the filings you downloaded in `<local_dir>/<ISIN>/` named
`<filing_type>__<period>__<YYYY-MM-DD>.<pdf|txt|htm|html>`, e.g.
`results__Q2FY27__2026-10-16.pdf` or `annual_report__FY2025.pdf` (date optional).
When `local_dir` is set it is used instead of `index_url`/`allowed_hosts`. `local_tier` gives the
files their source tier (2 default; use 1 only for official exchange/company filings).
Scanned/image-only PDFs have no text layer and are skipped (marked stale).

## 3. Exchange filings access (`src/ier/tools/config/exchange_filings.yaml`)

The connector is marked UNVERIFIED. Before any real run:

1. Confirm that programmatic access to NSE/BSE filing files is permitted by their terms and what
   rate limits apply. Respect them (`config/tools.yaml` `min_interval_seconds` is per host).
2. Provide a filings index: a URL returning a JSON list per company. `index_url` contains `{isin}`;
   each item is `{"url", "filing_type", "period", "published_date": "YYYY-MM-DD"}`. This may be a
   small service or static files you build from the exchange's own files.
3. Replace the placeholder `index_url` (`https://filings.invalid/...` does not resolve), and fill
   `allowed_hosts` with the verified exchange hosts as a mapping `host: tier`, e.g.
   `www.bseindia.com: 1`. While it is empty every filing fetch is REFUSED with a clear error
   (company runs fail at the fundamentals step). The index host, every filing host and every
   redirect hop must be listed; private/loopback/metadata addresses are always refused; responses
   over `max_response_bytes` (`config/tools.yaml`) are refused.
4. Remember `Source.tier`: it comes from the host's `allowed_hosts` entry (official exchange
   filings: tier 1). Press/ratings you add yourself must be tier 2; anything from forums is tier 3
   and can never support a claim or a fact (the rules engine ignores tier 3 facts; facts stored
   from tier 3 before this was enforced can be removed with
   `python -m ier.runtime purge-tier3` (dry run) then `--apply`).

## 4. Load the universe and seed the agents

```
# classification CSV columns: isin,nse_code,bse_code,name,exchange,macro_sector,sector,industry,basic_industry
python -m ier.runtime load-classification path/to/classification.csv
```
The universe filter also needs `listing_date`, `avg_daily_value` and `status` on each instrument
(`config/universe.yaml`); nothing in this repo fills those from market data yet, so set them
(SQL or your own loader) for the companies you want in the slice, or they are excluded.

```
python -m ier.runtime seed "Information Technology"
```

## 5. Run it

Terminal A, the workers (Ctrl-C to stop; `--drain` exits when no runnable job is left, with a
non-zero status if any job failed or is still queued, e.g. waiting in retry `backoff`). Workers
recover jobs left `running` by a crashed process (at start and every few minutes,
`config/runtime.yaml` `stale`). A failed sector job does not block its industries and companies:
they run with the most recent stored sector view of ANY age, or without a tailwind if none exists
(it is not flagged as stale; known limitation). The console shows `blocked` for a job
waiting on its parent job.
```
python -m ier.runtime workers --company 2          # sector/industry default to config/runtime.yaml
```
Terminal B, the API (127.0.0.1:8000):
```
python -m ier.api
```
Terminal C, the dashboard (http://localhost:3000; the API allows only this origin):
```
cd dashboard && npm install && npm run dev
```
Terminal D, queue a run (the workers pick it up; sector, then industry, then companies):
```
python -m ier.runtime plan "Information Technology" --run-id it-first-run
```
Watch the console in the dashboard. Unchanged inputs are skipped on later runs (the fingerprint
covers filings, sources, facts, it.yaml, signals.yaml, synthesis.yaml, the model names in
config/models.yaml and `SCORING_CODE_VERSION` in src/ier/rules/engine.py, so recalibrating any of
them re-runs everything). To force a rerun of one company use the dashboard re-run
(`POST /agents/{id}/rerun`, which always queues the job with payload `{"force": true}`).

One-shot alternative that also checks the invariants (claims have tier 1/2 evidence, no agent
left running) and prints a summary table:
```
IER_LIVE=1 IER_DATABASE_URL=postgresql+psycopg://postgres@localhost:5432/ier \
  pytest tests/golden/test_live.py -m live -s
```
Optional env: `IER_LIVE_MACRO` (default "Information Technology"), `IER_LIVE_TIMEOUT` seconds.
This test is skipped in every normal `pytest` run.

Expect the first real run to turn up prompt and rule fixes (a weaker model may produce claims the
verifier drops, facts with wrong periods, etc.). That list is the real output of Task 17 step 3.

## 6. Replace the synthetic YAML with your hand-checked real set

For 5 real IT companies (aim for a spread: strong, middling, weak, one with a real red flag such as
high promoter pledging or an auditor qualification, one with thin disclosure):

1. From the filings themselves (not from the system), write down for each: operating margin,
   attrition, revenue growth, debt/equity, promoter pledging (the facts `it.yaml` reads), and the
   period each refers to.
2. Work out the sub-scores, long/short scores and signal by hand using the formulas in the
   header of `tests/golden/it_golden.yaml`, with the benchmarks you settled on in section 7. Write
   the arithmetic as comments, as the synthetic file does.
3. Decide your own judgement of each company (rating you would defend), and note where it
   disagrees with the arithmetic. Disagreements are the signal that a rule or benchmark is wrong.
4. Record in a new YAML (e.g. `tests/golden/it_golden_real.yaml`): isin, the fact values, expected
   sub-score ranges, red flags, confidence and rating. Widen ranges only for real uncertainty
   (rounding, period choice), not to make a run pass.
5. The harness stubs the model from the YAML, so a real-data file cannot just replace the
   synthetic one. Use it as the expected side of `test_live.py`: add a test that, after the live
   run, loads your YAML and compares each company's stored scorecard and signal with it (copy the
   comparison from `test_golden_scores_within_ranges`). Keep the synthetic set: it still guards the
   plumbing in CI.

## 7. Calibrate the placeholders

The complete list, with file and key for each value, is [docs/PLACEHOLDERS.md](../../docs/PLACEHOLDERS.md).
Everything below is marked PLACEHOLDER in the files. Change them deliberately, bump the version
string in the same file, and rerun `pytest` (the synthetic golden ranges WILL fail when you change a
benchmark: update the hand arithmetic in the YAML comments and the ranges together, never only the
ranges).

* `src/ier/rules/config/it.yaml` (`version`: bump it): the benchmarks (operating margin 22,
  attrition 15, revenue growth 10, debt/equity 0.1, pledging 2, tailwind 50), the weights, the
  red-flag threshold (pledging > 20). Prefer a per-company-set median via the
  `<metric>_industry_median` Facts to a single constant. valuation and momentum have no IT rules
  yet, so those sub-scores are always empty.
* `ier.rules.engine.score_value`: the scale (50 at benchmark, +/-50 per 100% relative deviation,
  clamped). It is very steep for ratios with a small benchmark: debt/equity 0.2 already scores 0.
  Choose a scale that fits the metrics, in code, with a test, and bump `SCORING_CODE_VERSION`
  in the same module so unchanged companies are re-scored.
* `src/ier/rules/config/signals.yaml` (`version`): long/short weights (0.6/0.4), band edges
  (70/50/30), the confidence gate, the red-flag cap (hold).
* `src/ier/agents/config/synthesis.yaml`: verifier overlap thresholds and the confidence rules.
  Note that "high" confidence is currently unreachable: it needs at most 1 empty sub-score and
  valuation and momentum are always empty. Either add rules for them or relax
  `high_max_none_sub_scores`.
* `src/ier/api/config/api.yaml`: `stale_after_days`.

A suggested loop: run section 5, compare against your hand-checked set, fix the single worst
disagreement, rerun. The signal labels are research labels; nothing in the system places orders.

## 3b. Live mode (file watcher)

`python -m ier.runtime watch` runs the workers and, every 15 s, scans `local_dir`. When an ISIN
folder's files change (new, edited or removed), that company's agent is queued; a run whose
filings it already processed skips itself without calling a model. Drop a file in
`data\filings\<ISIN>\` and the run starts within the scan interval. Keep Postgres and the
environment variables set as for `workers`. Stop with Ctrl-C.

### 3c. Price, news and social files

Besides filings, each company folder can hold three kinds of feed files. Name them
`<kind>__<name>__YYYY-MM-DD.<ext>` inside `data\filings\<ISIN>\`:

| Kind | Example | What it does |
|------|---------|--------------|
| prices | `prices__daily__2026-11-13.csv` | Daily CSV with date, close and (optional) volume columns. Feeds the short-term score. Needs ~200 rows for the 200-day average. |
| news | `news__announcements__2026-11-12.txt` (also .pdf .html .csv) | Events listed on the company page. Does not change the score. |
| social | `social__x__2026-11-12.csv` (also .txt .json) | Same, marked unverified. Does not change the score. |

The watcher wakes the company on a new or changed feed file. A news or social file does not
repeat the filings extraction (no extra model tokens for the financials).

### 3d. Zerodha prices (read-only, replaces the manual NSE download)

Needs the paid Kite Connect app (₹500/month; the free Personal app has no data). Personal research
use only: do not show or share Kite data. The tool can only list NSE instruments and download
daily candles; there is no code for orders, positions or funds.

1. Once: set `IER_KITE_API_KEY` and `IER_KITE_API_SECRET` (no quotes) in the window, and set the
   app's redirect URL in the Kite developer console to `http://127.0.0.1` (any address you can
   copy from the browser works). Edit `config/zerodha.yaml` to list your symbols.
2. Every trading day (the token ends at 6 AM IST): `python -m ier.runtime kite-login`. Open the
   link, log in, copy the address you are redirected to, paste it back. Keys and tokens are never
   printed.
3. `python -m ier.runtime kite-prices` writes `data\filings\<ISIN>\prices__zerodha__<date>.csv`
   for each symbol (one file per company, replaced when the data changes). With `watch` running,
   each company re-runs and its short-term score updates. Add `--symbols INFY,TCS` to pick a few,
   `--loop 30` to repeat every 30 minutes, `--intraday` to include today's unfinished candle.
   Intraday re-runs the company on every change, which spends model tokens each time.

## 3e. Segment controllers and live sources

Six driver segments (company, events, market flows, sector and peers, macro, news and sentiment)
each have a controller agent under the master; each source in `config/sources.yaml` has a source
agent under its controller. **All display-only: no source changes any score.** No model calls.

First time (and after pulling this update), in every new window set IER_DATABASE_URL, then:

    python -m alembic upgrade head
    python -m ier.runtime seed "Information Technology"     # also creates controllers + source agents

Run the watcher as before (`python -m ier.runtime watch`). It now also queues a check for each
source when its refresh interval passes. Open http://localhost:3000/sources: each source shows
its connection status and a Test button (runs the check right now, even without the watcher).

What each source needs:

| Source | Needs |
|---|---|
| Zerodha prices | `kite-login` once a day |
| Filings, announcements, X export | files you drop in `<local_dir>/<ISIN>/` |
| FII/DII, Nifty IT, India VIX, estimates, corporate actions | a CSV (Date,Close) in `data/sources/<name>/` |
| USD/INR | nothing (public ECB rates, no key) |
| FRED (US 10-year, Fed funds, Nasdaq, Brent) | free key: `set IER_FRED_API_KEY=...` (no quotes) |
| News headlines | nothing; writes `news__rss-google__<date>.txt` per company, which the news agent reads. Terms UNVERIFIED |
| Peer ranking | scorecards for 2+ companies |

Not connected on purpose: Moneycontrol (no scraping, terms unverified), X API (paid), NSE/BSE
site feeds (terms unverified). The database pool is now 20 + 20 connections (IER_DB_POOL_SIZE,
IER_DB_MAX_OVERFLOW), which fixes the "QueuePool limit" error.

## 3f. Second sector: Indian pharma (NSE macro sector "Healthcare")

Rules are chosen per company from its macro sector: `Healthcare` uses
`src/ier/rules/config/pharma.yaml` (operating margin, R&D intensity, USFDA Form 483 observations,
revenue growth, debt/equity, pledging, tailwind, momentum); every other sector keeps the IT rules.
All pharma benchmarks are PLACEHOLDERS, uncalibrated, and the new sources are display-only.

1. In your NSE classification CSV, keep the rows for the 5 pharma companies you want (e.g. Sun Pharma,
   Dr Reddy's, Cipla, Lupin, Divi's) and save them as `classification_pharma.csv`. Check the
   `macro_sector` cell: it must read exactly `Healthcare` (the loader keeps what the file says).
2. `python -m ier.runtime load-classification classification_pharma.csv`
3. `python -m ier.runtime seed "Healthcare"` then `python -m ier.runtime plan "Healthcare"`
4. Put each company's results / transcript / shareholding files in `data/filings/<ISIN>/` as for IT.
   Annual reports and results PDFs are what carry R&D % of sales and USFDA observations.
5. Optional: export Nifty Pharma history from the NSE historical index page into
   `data/sources/nifty_pharma/` (Date,Close); see it on the Sources page.
