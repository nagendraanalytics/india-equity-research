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
