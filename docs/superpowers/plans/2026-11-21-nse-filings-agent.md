# NSE filings agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `python -m ier.runtime nse-filings` downloads quarterly results and annual reports from NSE into `<local_dir>/<ISIN>/`, run by two CronJobs.

**Architecture:** One module `src/ier/tools/nse_filings.py` modelled on `nse_prices.py` (config loader, injectable httpx client, an `update_*` function returning one result dict per company). Output files use the layout `local_filings` already reads, so the company graph and the `watch` command need no change.

**Tech Stack:** Python, httpx (MockTransport in tests), PyYAML, SQLAlchemy, pytest, Kubernetes CronJob YAML.

**Spec:** `docs/superpowers/specs/2026-11-21-nse-filings-agent-design.md`

## Global Constraints

- Output path: `<local_dir>/<ISIN>/<filing_type>__<period>__<YYYY-MM-DD>.pdf`; `filing_type` is `results` or `annual_report`; period is `Q<n>FY<yy>` (e.g. `Q2FY27`) or `FY<yyyy>` (e.g. `FY2026`).
- Allowed hosts, exactly: `www.nseindia.com`, `nsearchives.nseindia.com`, `archives.nseindia.com`. Redirects are refused (no `follow_redirects`).
- Saved bodies must start with `%PDF`; caps: results 20 MB, annual reports 60 MB (config keys `max_mb_results`, `max_mb_annual`).
- Retention: results 2 years, annual reports 3 financial years (config `results_years`, `annual_years`).
- Min gap between requests 1.0 s (`min_gap_seconds`). Read-only; no writes outside `<local_dir>/<ISIN>/`.
- Result statuses are exactly `written | unchanged | nodata | blocked | error`.
- NSE terms of use, endpoint paths and JSON field names are UNVERIFIED and must be marked so in config comments and `docs/PLACEHOLDERS.md`.
- Same CronJob shape as `deploy/k8s/nse-prices.yaml` (image `ghcr.io/nagendraanalytics/ier-api`, secret `ier-secrets`, PVC `ier-data`, user 65534).
- Never add filings or prices to git.

## Review Focus

- NSE returns an HTML/JSON error page with HTTP 200 for a blocked session: nothing may be saved as `.pdf`; the status is `blocked` or `error`.
- An attachment URL pointing off-list (or redirecting off-list): skipped and reported, never fetched.
- An instrument with no `nse_code`, or an unknown `--symbols` value: skipped or reported as `error` for that symbol alone; the other companies continue.
- A crash half-way through a download: no truncated file is left that a later run would treat as done (atomic write).
- Re-running the job the same day: every file reports `unchanged`, nothing is downloaded twice.

---

### Task 1: Config, naming and safety helpers

**Files:**
- Create: `src/ier/tools/nse_filings.py`, `config/nse_filings.yaml`
- Test: `tests/tools/test_nse_filings.py`

**Interfaces:**
- Produces:
  - `load_config(path: Path = CONFIG_PATH) -> dict` (defaults merged with YAML, like `nse_prices.load_config`)
  - `class NseError(Exception)`, `class NseBlocked(NseError)`
  - `quarter_label(period_end: date) -> str` (31 Mar 2026 -> `Q4FY26`; 30 Jun 2026 -> `Q1FY27`; Indian FY starts 1 April)
  - `fy_label(year_end: date) -> str` (31 Mar 2026 -> `FY2026`)
  - `filing_name(kind: str, period: str, published: date) -> str` -> `results__Q2FY27__2026-10-16.pdf`
  - `host_allowed(url: str, cfg: dict) -> bool` (https only, host in `cfg["allowed_hosts"]`)
  - `looks_like_pdf(body: bytes) -> bool`

- [ ] **Step 1: Write the failing tests** `test_quarter_label_indian_fy` (Mar->Q4FY26, Jun->Q1FY27, Sep->Q2FY27, Dec->Q3FY27), `test_fy_label`, `test_filing_name`, `test_host_allowed_rejects_http_other_hosts_and_lookalikes` (`https://nsearchives.nseindia.com.evil.com/x.pdf` is rejected), `test_looks_like_pdf` (`b"%PDF-1.7..."` true, `b"<html>"` false, `b""` false), `test_load_config_defaults`.
- [ ] **Step 2: Run** `PYTHONPATH=src .venv/bin/python -m pytest tests/tools/test_nse_filings.py -q`. Expected: FAIL (module missing).
- [ ] **Step 3: Implement** the functions above in `src/ier/tools/nse_filings.py`; create `config/nse_filings.yaml` with every key optional and the UNVERIFIED comment (keys: `results_years: 2`, `annual_years: 3`, `min_gap_seconds: 1.0`, `max_mb_results: 20`, `max_mb_annual: 60`, `allowed_hosts`, `user_agent`, `timeout_seconds: 30`, and an `endpoints` block with the three paths from the spec).
- [ ] **Step 4: Run** the same command. Expected: PASS.
- [ ] **Step 5: Commit** `git add src/ier/tools/nse_filings.py config/nse_filings.yaml tests/tools/test_nse_filings.py && git commit -m "feat: nse filings helpers and config"`

### Task 2: NSE client (session, JSON, download)

**Files:**
- Modify: `src/ier/tools/nse_filings.py`
- Test: `tests/tools/test_nse_filings.py`

**Interfaces:**
- Consumes: Task 1 `NseError`, `NseBlocked`, `host_allowed`, `looks_like_pdf`, `load_config`.
- Produces:
  - `NseFilingsClient(cfg: dict, *, transport: httpx.BaseTransport | None = None, sleep=time.sleep)`
  - `.get_json(path_and_query: str) -> list | dict`: opens the cookie session once (GET `https://www.nseindia.com/`), then GETs `https://www.nseindia.com` + path. HTTP 401/403 -> `NseBlocked`; a 200 whose body is not JSON -> `NseBlocked("NSE returned a page instead of data (session refused?)")`; 5xx/timeout retried twice then `NseError`.
  - `.download(url: str, max_bytes: int) -> bytes | None`: refuses off-list hosts (returns `None`), refuses redirects (`NseError`), enforces `max_bytes` while streaming (returns `None` when exceeded), returns bytes only if `looks_like_pdf`, else `None`.

- [ ] **Step 1: Write the failing tests** with `httpx.MockTransport`: `test_session_opened_once_then_json`, `test_403_raises_blocked`, `test_html_with_200_raises_blocked`, `test_retries_5xx_then_error`, `test_download_ok_pdf`, `test_download_html_body_returns_none`, `test_download_off_list_host_not_requested` (assert the mock saw no request), `test_download_redirect_refused`, `test_download_too_large_returns_none`, `test_min_gap_uses_injected_sleep`.
- [ ] **Step 2: Run** the test file. Expected: new tests FAIL (class missing).
- [ ] **Step 3: Implement `NseFilingsClient`** following `NseClient` in `nse_prices.py` (same httpx options, `follow_redirects=False`); send browser-like headers from `cfg["user_agent"]` plus `Accept` and `Referer: https://www.nseindia.com/`; enforce the gap with the injected `sleep`.
- [ ] **Step 4: Run** the test file. Expected: all PASS.
- [ ] **Step 5: Commit** `git commit -am "feat: nse filings client"` (after `git add` of both files).

### Task 3: `update_filings`

**Files:**
- Modify: `src/ier/tools/nse_filings.py`
- Test: `tests/tools/test_nse_filings.py`

**Interfaces:**
- Consumes: Task 1 helpers, Task 2 `NseFilingsClient`, `ier.db.models.Instrument` (`isin`, `nse_code`), `ier.tools.local_filings.ISIN_RE`.
- Produces: `update_filings(session, client, cfg: dict, local_dir: str, *, now: datetime, symbols: list[str] | None = None, kinds: tuple[str, ...] = ("results", "annual")) -> list[dict]`. One dict per company and kind: `{"symbol","isin","kind","status","files": int,"error": str|None}`. A `NseBlocked` aborts the whole run: that company and kind are `blocked`, the remaining ones are not attempted and come back `blocked` too.
- Normalisation helper `parse_items(kind: str, raw) -> list[dict]` returns `{"period","published","url"}` and tolerates the plausible field names (`attchmntFile`/`attachment`/`xbrl` for the URL, `broadcastDate`/`an_dt`/`date` for the date, `toDate`/`qe_Date`/`to_date` for the period end), skipping items with no URL or date. Confirm the real names on the server; they are listed in one dict at the top of the module.

- [ ] **Step 1: Write the failing tests** using a fake client (a small class with `get_json` and `download` returning canned data) and the `session` fixture used by `tests/tools/test_nse_prices.py`: `test_writes_results_and_annual_files_with_expected_names`, `test_second_run_reports_unchanged_and_downloads_nothing`, `test_existing_file_is_not_overwritten`, `test_unknown_symbol_is_error_others_continue`, `test_instrument_without_nse_code_is_skipped`, `test_off_list_url_skipped_and_reported`, `test_html_body_never_saved` (download returns `None`), `test_blocked_aborts_rest_and_marks_blocked`, `test_keeps_only_last_n_years`, `test_write_is_atomic_no_tmp_left_after_failure`.
- [ ] **Step 2: Run** the test file. Expected: new tests FAIL.
- [ ] **Step 3: Implement** `parse_items` and `update_filings`: per company, list items via `client.get_json` (endpoint from `cfg["endpoints"]`, `{symbol}` substituted, URL-encoded), keep the last `results_years`/`annual_years`, skip names that already exist, download, write to `<name>.tmp` in the ISIN folder and `os.replace` into place (remove the temp on any exception). `nodata` when NSE lists nothing in range.
- [ ] **Step 4: Run** the test file. Expected: all PASS.
- [ ] **Step 5: Commit** `git commit -am "feat: update_filings"`.

### Task 4: CLI, CronJobs and docs

**Files:**
- Modify: `src/ier/runtime/__main__.py`, `deploy/k8s/kustomization.yaml`, `docs/PLACEHOLDERS.md`, `docs/DEPLOYMENT.md`, `README.md`
- Create: `deploy/k8s/nse-filings.yaml`
- Test: `tests/tools/test_nse_filings.py`

**Interfaces:**
- Consumes: Task 3 `update_filings`, `NseFilingsClient`, `NseError`, `NseBlocked`, `load_config`.
- Produces: subcommand `nse-filings [--symbols A,B] [--kind results|annual|all] [--loop MINUTES]` handled by `_nse_filings(args, factory, *, client=None, now_fn=None) -> int`, same shape as `_nse_prices`: prints `<symbol> <isin> <kind> <status> <files or error>` per line; returns 1 if any status is `blocked` or `error`, else 0; `--loop` below 5 exits with an error as in `_nse_prices`.

- [ ] **Step 1: Write the failing tests** `test_cli_prints_one_line_per_company_and_kind`, `test_cli_exit_code_1_when_blocked`, `test_cli_kind_results_only_requests_results`, `test_cli_loop_below_5_rejected`, calling `_nse_filings` with the fake client and the DB `factory` fixture like the `nse-prices` CLI test.
- [ ] **Step 2: Run** the test file. Expected: new tests FAIL.
- [ ] **Step 3: Implement** `_nse_filings`, the parser entry, and the dispatch line next to `nse-prices`; reuse the `local_dir` check and `mkdir`.
- [ ] **Step 4: Create** `deploy/k8s/nse-filings.yaml` with two CronJobs copied from `nse-prices.yaml`: `nse-filings-results` (`30 14 * * 1-5`, command `["python","-m","ier.runtime","nse-filings","--kind","results"]`) and `nse-filings-annual` (`0 3 * * 0`, `--kind annual`); add `- nse-filings.yaml` to `deploy/k8s/kustomization.yaml`.
- [ ] **Step 5: Docs.** `docs/PLACEHOLDERS.md`: UNVERIFIED NSE terms, endpoints, field names, IP blocking. `docs/DEPLOYMENT.md`: how to run `nse-filings --symbols ITC --kind results` first on the server, and the GitHub flow (push from Windows, CI builds the image, Argo CD syncs `deploy/k8s`, `kubectl rollout status`), stating that filings and prices stay on the `ier-data` volume and out of git. `README.md`: one line for the new command.
- [ ] **Step 6: Run the whole suite** `PYTHONPATH=src .venv/bin/python -m pytest -q`. Expected: all PASS (restart the test Postgres first if DB tests error).
- [ ] **Step 7: Commit** `git add -A src deploy docs README.md tests config && git commit -m "feat: nse-filings command, CronJobs and docs"`.
