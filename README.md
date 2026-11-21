# India equity research agents

Personal research system for NSE/BSE equities: agents read official filings, a deterministic rules
engine scores companies, and a dashboard shows scorecards, evidence and research labels. You decide;
nothing here places, sizes or routes an order, and the labels are not investment advice.

* Setup and the operator's runbook: [tests/golden/RUNBOOK.md](tests/golden/RUNBOOK.md)
* Every uncalibrated value: [docs/PLACEHOLDERS.md](docs/PLACEHOLDERS.md)
* Design: [docs/superpowers/specs/2026-11-06-india-equity-research-agents-design.md](docs/superpowers/specs/2026-11-06-india-equity-research-agents-design.md)

```
python -m venv .venv && . .venv/bin/activate
pip install -e .          # editable install is required (see 5 below)
pytest                    # needs a Postgres test database, see tests/db/conftest.py
cd dashboard && npm install && npm test
```

## Know these limits before you rely on anything

1. **Not autonomous yet.** The triggers `on_new_source`, `on_market_event` and `on_schedule`
   exist but nothing calls them. Runs happen only via `python -m ier.runtime plan ...` or a manual
   re-run from the dashboard.
2. **"Short-term" is not a timing layer.** The short-term score is attrition plus promoter
   pledging. Technicals, news and macro agents are deferred.
3. **Freshness is the scorecard run date, not data age.** Unchanged filings are carried forward,
   so a fresh date can sit on old data.
4. **Industry ranking reflects the previous run** (the industry job runs before its companies).
5. **Use `pip install -e .`.** pyproject package-data covers only `ier.api`, so a regular
   install misses the other config files.
6. **`POST /agents/{id}/rerun` has no authentication.** Keep the API on 127.0.0.1; never expose
   it beyond loopback (leave `IER_API_HOST` unset or 127.0.0.1).
7. **Filing fetches are refused until you verify the exchange hosts** and list them in
   `src/ier/tools/config/exchange_filings.yaml` `allowed_hosts` (host: tier).

## Algorithm library

25 deterministic price-based algorithms (trend, momentum, risk, breakout, volume, relative strength) plus 8
phase-2 fundamental stubs run inside the `technicals` step and are stored in `algo_scores`. The dashboard's
**Algorithms** menu shows per-stock scoreboards and charts, sector rankings, an overview and a page per
algorithm. Algorithm scores reach the final scores only through `blend_weight` in `config/algos.yaml`
(default 0 = off, hard maximum 0.30). All bands are uncalibrated placeholders; see `docs/PLACEHOLDERS.md`.

## Price history for the algorithms
`python -m ier.runtime nse-prices` downloads daily prices (first run about 290 trading days, later runs one day)
into `<local_dir>/<ISIN>/prices__nse__<date>.csv` for every company that has an NSE code. Run it each evening after
18:30 IST; the watcher then re-runs the changed companies. `kite-prices` (Zerodha) remains an alternative source.

## Filings for the fundamentals
`python -m ier.runtime nse-filings [--kind results|annual|all]` downloads quarterly results and annual reports from NSE into
`<local_dir>/<ISIN>/` (the layout `local_filings` reads), skipping files already there. NSE blocks the server address for this part of its site, so run it from a home PC instead:
`python -m ier.tools.nse_filings --isin ITC=INE154A01025,DABUR=INE016A01026 --out C:\filings` (needs only `httpx` and `pyyaml`),
then copy the folder to the server; see `docs/DEPLOYMENT.md`. The two server CronJobs are suspended.
