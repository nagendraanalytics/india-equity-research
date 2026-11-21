# Deployment plan: GitHub -> GitHub Actions -> Argo CD -> Hetzner VPS

Status: step 1 (GitHub) ready. Steps 2-4 are not built yet.

## Decisions taken (change any of them)
- Private GitHub repo only. The history holds no secrets (scanned) and `data/` is git-ignored.
- Access: only the owner. No public port to the API (it has no login and its re-run endpoint
  changes state). The dashboard is reached through a private tunnel (Tailscale) or a login proxy.
- One small Kubernetes (k3s) node on Hetzner, managed by Argo CD from a `deploy/` folder in the repo.
- Secrets (Groq key, Kite key/secret, DB password) never go in git: Kubernetes Secrets created
  by hand or sealed with SOPS/Sealed Secrets.

## Steps
1. GitHub: create private repo, push `master`. Add branch protection later.
2. GitHub Actions: on every push run the Python tests (Postgres+pgvector service) and the
   dashboard tests plus type check; on `master` build `api` and `dashboard` images and push to GHCR.
3. Containers and manifests: Dockerfile for the API/watcher (one image, two commands), Dockerfile
   for the dashboard, Kustomize manifests under `deploy/` (Postgres+pgvector StatefulSet with a
   volume, API, watcher, dashboard, a volume for `data/filings`).
4. Hetzner: create a server, install k3s and Argo CD, apply one Argo `Application` pointing at
   `deploy/`. After that a push to `master` deploys by itself.

## Open items that need your decision before step 3
- Filings are hand-downloaded PDFs. On the VPS they need an upload path (scp or a small upload
  page) into the filings volume.
- Zerodha login is interactive and expires at 6 AM IST daily; a server cannot do it unattended.
  Keep Kite prices on your PC and upload the price CSVs, or accept a manual daily login.
- Kite data is personal-use only; exchange site terms are still unverified. Review before any
  shared use.
- Groq free tier caps (200k tokens/day) apply on the server too.

## Migration note: algorithm library (alembic 0012)
Adds the `algo_scores` table and five nullable columns on `scorecards`
(`rules_long_score`, `rules_short_score`, `algo_long_score`, `algo_short_score`, `algo_weight`).
It is additive and reversible (`alembic downgrade 0011`). Run `alembic upgrade head` before starting the new API image.
Existing companies get algorithm scores on their next run. Keep `blend_weight: 0.0` until backtested.

## Getting code and data onto the server (GitHub flow)
* **Code and config: GitHub to server (pull).** Push from your Windows folder (`C:\projects\ier-clean`) to `master`. The
  release workflow builds the image and rewrites the tags in `deploy/k8s/kustomization.yaml`; Argo CD syncs `deploy/k8s`
  and rolls the pods. Check with `kubectl get pods -n ier` and `kubectl rollout status deploy/api -n ier`.
  The server never pushes to GitHub; that would need a deploy key on the server, so it is deferred until there is something
  small worth pushing.
* **Data stays off git.** Filings, price CSVs and the database live on the `ier-data` volume and in Postgres.
* **Filings from a home PC.** NSE returns HTTP 403 to the server for the filings listing, so the CronJobs
  `nse-filings-results` and `nse-filings-annual` are suspended. Instead, on your Windows PC (Python with
  `pip install httpx pyyaml`, repo folder as the current directory):
  ```
  set PYTHONPATH=src
  python -m ier.tools.nse_filings --isin HINDUNILVR=INE030A01027,ITC=INE154A01025,NESTLEIND=INE239A01024,BRITANNIA=INE216A01030,DABUR=INE016A01026,MARICO=INE196A01026 --out C:\filings
  scp -r C:\filings ier@2.29.197.65:~/
  ```
  Then on the server, stream the folder into the api pod's volume (no `tar` is needed inside the pod):
  ```
  tar -C ~/filings -cf - . | kubectl exec -i -n ier deploy/api -c api -- python -c "import sys,tarfile; tarfile.open(fileobj=sys.stdin.buffer, mode='r|').extractall('/app/data/filings')"
  ```
  Files already on the server are overwritten with identical copies; the watcher then queues the changed companies.
  The command prints `blocked` if NSE refuses your home connection too; then place the PDFs by hand.
