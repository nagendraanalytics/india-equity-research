# Placeholders: every uncalibrated value in one list

Nothing below has been researched or calibrated. Each value is a guess chosen so the system runs.
Until you set them deliberately (RUNBOOK section 7), scores, labels, limits and staleness flags
are not meaningful. Change a value on purpose, bump the `version` in the same file where there is
one, and rerun `pytest` (the synthetic golden ranges fail on purpose when a benchmark changes).
Changing `it.yaml`, `signals.yaml`, `synthesis.yaml`, the model names or `SCORING_CODE_VERSION`
also changes the run fingerprint, so the next run re-scores every company.

| File | Key | Shipped value | What it controls |
|---|---|---|---|
| `src/ier/rules/config/it.yaml` | `rules[].benchmark` | operating_margin 22, attrition 15, revenue_growth 10, debt_equity 0.1, promoter_pledging 2, sector_tailwind 50 | the value that scores 50 |
| `src/ier/rules/config/it.yaml` | `rules[].weight` | 1.0 each, sector_tailwind 0.25 | weight in the sub-score and horizon averages |
| `src/ier/rules/config/it.yaml` | `rules[].horizon` / `sub_score` | as shipped | which score each metric feeds; valuation and momentum have no IT rules (always empty) |
| `src/ier/rules/config/it.yaml` | `red_flags[]` | promoter_pledging > 20 | red-flag threshold |
| `src/ier/rules/engine.py` | `score_value` scale (steepness) | 50 at benchmark, +/-50 per 100% relative deviation, clamped 0-100 | very steep for small benchmarks (debt/equity 0.2 scores 0); changing it means bumping `SCORING_CODE_VERSION` |
| `src/ier/rules/config/signals.yaml` | `bands` | accumulate >= 70, hold >= 50, reduce >= 30, else avoid | research label bands |
| `src/ier/rules/config/signals.yaml` | `weights` | long 0.6, short 0.4 | long/short combination |
| `src/ier/rules/config/signals.yaml` | `confidence_gate.insufficient_confidence` | [low] | which confidence gives "insufficient evidence" |
| `src/ier/rules/config/signals.yaml` | `red_flag_cap.max_rating` | hold | best label allowed with a red flag |
| `src/ier/agents/config/synthesis.yaml` | `verifier.overlap_low` / `overlap_high` | 0.4 / 0.75 | claim support thresholds (between: model check) |
| `src/ier/agents/config/synthesis.yaml` | `confidence.*` | min 3 chunks, max 3 empty sub-scores; high: 8 chunks, max 1 empty | scorecard confidence ("high" is unreachable today: valuation and momentum are always empty) |
| `src/ier/api/config/api.yaml` | `stale_after_days` | 7 | when the API flags an as-of date stale (see README: this is the scorecard run date, not data age) |
| `config/universe.yaml` | `min_avg_daily_value` | 10000000 INR (1 crore) | liquidity floor for the universe |
| `config/universe.yaml` | `excluded_statuses` | [suspended] | statuses excluded from the universe (surveillance-flagged and data-poor instruments are excluded in code) |
| `src/ier/universe/filter.py` | `min_listing_days` default | 365 | minimum listing age |
| `config/runtime.yaml` | `retry.max_attempts` / `retry.backoff_base_seconds` | 3 / 30 | job retries and exponential backoff |
| `config/runtime.yaml` | `workers` | sector 1, industry 1, company 2 | worker threads per tier |
| `config/runtime.yaml` | `stale.after_seconds` / `stale.check_interval_seconds` | 7200 / 300 | when a running job is presumed lost and recovered; must exceed the longest real job |
| `config/tools.yaml` | `min_interval_seconds` | 1.0 | minimum gap between requests to one host (check the exchange's terms) |
| `config/tools.yaml` | `max_response_bytes` | 20971520 (20 MB) | larger responses are refused |
| `config/tools.yaml` | `timeout_seconds` | 30 | per-request fetch timeout |
| `config/models.yaml` | `caps.per_run` | 200000 tokens, 1800 model-seconds | budget of ONE company job (or one sector job) in a run; hitting it fails that job only |
| `config/models.yaml` | `caps.per_day` | 1000000 tokens, 7200 model-seconds | daily budget (UTC); hitting it stops all workers until the next UTC midnight |
| `config/models.yaml` | `timeout_seconds`, `max_output_tokens`, `models.*` | 120, 2048, qwen2.5 14b/32b | model server settings |
| `src/ier/tools/config/exchange_filings.yaml` | `index_url` | `https://filings.invalid/...` | UNVERIFIED placeholder that does not resolve |
| `src/ier/tools/config/exchange_filings.yaml` | `allowed_hosts` | `{}` (empty) | host -> source tier; EMPTY REFUSES every filing fetch until you verify the exchange hosts and terms |

Residuals that are not values but you should know (see the module docstrings):

* Compute caps are checked before each model call, so each concurrently running worker can
  overshoot a cap by at most one call.
* Fetch checks the host's addresses before connecting but the connection resolves the name
  again, so DNS rebinding between the two is not prevented; an unresolvable host is not refused
  by the check (the connection then fails).
| `src/ier/tools/config/exchange_filings.yaml` | `local_dir` / `local_tier` | `""` / `2` | local-folder filings mode; you decide whether your downloads count as tier 1 (official) or 2 |
