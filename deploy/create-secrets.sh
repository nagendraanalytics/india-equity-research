#!/usr/bin/env bash
# Run ON THE SERVER as the ier user. Creates the two Kubernetes Secrets by prompting for each
# value (typing is hidden). Nothing is written to a file or to git. Re-running replaces them.
set -euo pipefail

ask() {  # ask VAR "prompt" [optional]
  local v
  read -r -s -p "$2: " v; echo
  if [ -z "$v" ] && [ "${3:-}" != "optional" ]; then echo "Value required." >&2; exit 1; fi
  printf -v "$1" '%s' "$v"
}

kubectl create namespace ier --dry-run=client -o yaml | kubectl apply -f -

ask PGPASS   "New database password (make one up, letters and digits only)"
ask GROQ     "Groq API key"
ask KITE_KEY "Kite API key (Enter to skip)" optional
ask KITE_SEC "Kite API secret (Enter to skip)" optional
ask FRED     "FRED API key (Enter to skip)" optional

kubectl -n ier create secret generic ier-secrets \
  --from-literal=POSTGRES_PASSWORD="$PGPASS" \
  --from-literal=IER_DATABASE_URL="postgresql+psycopg://postgres:${PGPASS}@postgres:5432/ier" \
  --from-literal=IER_LLM_API_KEY="$GROQ" \
  --from-literal=IER_KITE_API_KEY="$KITE_KEY" \
  --from-literal=IER_KITE_API_SECRET="$KITE_SEC" \
  --from-literal=IER_FRED_API_KEY="$FRED" \
  --dry-run=client -o yaml | kubectl apply -f -

echo
echo "Now the pull secret for the private GitHub image registry."
echo "Create a classic token at github.com/settings/tokens with ONLY the read:packages scope."
read -r -p "GitHub username: " GHUSER
ask GHTOKEN "GitHub token (read:packages)"
kubectl -n ier create secret docker-registry ghcr-pull \
  --docker-server=ghcr.io --docker-username="$GHUSER" --docker-password="$GHTOKEN" \
  --dry-run=client -o yaml | kubectl apply -f -

echo "Done. Secrets ier-secrets and ghcr-pull are in namespace ier."
