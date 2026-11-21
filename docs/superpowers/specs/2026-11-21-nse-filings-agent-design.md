# NSE filings agent: design

Status: draft for owner approval (2026-11-21)

## Why

FMCG (and every other sector) shows "No Score" because no company has filings in its
`<local_dir>/<ISIN>/` folder. Today filings only arrive by hand. The owner wants them fetched from
NSE (BSE later) automatically, quarterly results AND annual reports, and wants the server to stay
in sync with GitHub.

## Goals

- `python -m ier.runtime nse-filings` downloads, per instrument in the `instruments` table:
  - quarterly financial results (last 2 years),
  - annual reports (last 3 financial years),
  into `<local_dir>/<ISIN>/<filing_type>__<period>__<YYYY-MM-DD>.pdf`, the layout `local_filings`
  already reads. No change to the company graph is needed: the existing `watch` command queues a
  company run when its folder changes.
- Idempotent: a file already on disk is skipped. Writes are atomic (temp file, then rename).
- Two CronJobs: results daily, annual reports weekly. Same image, secret and `ier-data` PVC as
  `nse-prices`.
- Fails loudly and kindly: a 403/blocked session stops the run with a clear message, not a stack.

## Non-goals / anti-goals

- No BSE in this version. No paid APIs. No PDF parsing (the existing agents do that).
- Never commit filings or prices to git. They live on the `ier-data` volume.
- Never fetch from a host outside the allow-list, and never follow an off-list redirect.
- Not "working" if it silently downloads HTML error pages saved as `.pdf`.

## Approaches considered

1. **Own NSE fetcher (chosen).** Same shape as `nse_prices.py`. No new dependency. Risk: NSE can
   change or block its undocumented endpoints.
2. Community NSE/BSE Python package. Less code, but we inherit someone else's breakage.
3. Paid filings API. Most reliable, but costs money and adds a vendor.

## Design

- `src/ier/tools/nse_filings.py`, config `config/nse_filings.yaml`, tests in
  `tests/tools/test_nse_filings.py` using a fake client (no network in tests).
- `NseFilingsClient`: opens a session on `https://www.nseindia.com/` to obtain cookies, then calls
  JSON endpoints with browser-like headers (user agent is configurable). Minimum gap between
  requests (default 1 s). Retries only on 5xx/timeouts; 403 raises `NseBlocked`.
- Endpoints (UNVERIFIED, written from public descriptions, to be confirmed on the server; they
  live in config so a change needs no code edit):
  - announcements: `/api/corporate-announcements?index=equities&symbol=<S>&from_date&to_date`
  - financial results: `/api/corporates-financial-results?index=equities&symbol=<S>&period=Quarterly`
  - annual reports: `/api/annual-reports?index=equities&symbol=<S>`
- Attachment URLs must be on `nsearchives.nseindia.com` or `archives.nseindia.com`
  (`allowed_hosts` in the config); anything else is skipped and reported.
- Downloaded body must start with `%PDF` and be under a size cap (results 20 MB, annual reports
  60 MB, configurable), otherwise it is not saved.
- Period naming: results -> `Q<n>FY<yy>` from the quarter-end date; annual report -> `FY<yyyy>`.
  The published date is the filing date from NSE.
- Result statuses per company/kind: `written | unchanged | nodata | blocked | error`, printed as a
  short table, with a non-zero exit code only for `blocked` or `error`.
- CLI: `nse-filings [--symbols A,B] [--kind results|annual|all] [--years N] [--loop]`.
- Deploy: `deploy/k8s/nse-filings.yaml` with two CronJobs
  (`30 14 * * 1-5` results, `0 3 * * 0` annual); added to `kustomization.yaml`.
- Docs: `docs/PLACEHOLDERS.md` (UNVERIFIED terms and endpoints), `docs/DEPLOYMENT.md`
  (GitHub flow below), README.

## GitHub (pull only, for now)

The server gets new code from GitHub through the existing Argo CD sync of `deploy/k8s`. This work
documents the path (push from Windows, CI builds the image, Argo rolls it out) and checks it end to
end. Server-to-GitHub pushes are deferred until there are small text outputs worth saving; they
would need a deploy key on the server, which is a security cost not worth paying yet.

## Risks (owner to confirm before production use)

- NSE terms of use for programmatic access are UNVERIFIED.
- NSE may block data-centre IPs (Hetzner). The first thing to do on the server is run
  `nse-filings --symbols ITC --kind results` and read the status. If it is `blocked`, we fall back
  to hand-placed files or to approach 2/3.
- Endpoint paths and field names are from public descriptions and could differ; they are config.

## Left to the builder

Exact JSON field names (confirmed against the live response once reachable), the parsing of period
labels, and the retry/backoff constants.
