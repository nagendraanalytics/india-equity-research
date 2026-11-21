#!/usr/bin/env bash
# Run ON THE SERVER as the ier user. Creates secret web-login (the username and a bcrypt hash of
# the password) for the public nip.io address. The password is typed hidden, piped over stdin to a
# throwaway pod (never in a command line or file), and only its hash is stored. Re-run to change it.
set -euo pipefail
export KUBECONFIG="${KUBECONFIG:-$HOME/.kube/config}"
read -r -p "Login name: " U
read -r -s -p "Password (16+ characters): " P; echo
[ -n "$U" ] && [ "${#P}" -ge 16 ] || { echo "Need a name and a password of 16+ characters." >&2; exit 1; }
LINE=$(printf '%s\n' "$P" | kubectl -n ier run hashpw --rm -i -q --restart=Never --image=httpd:2-alpine \
        -- htpasswd -niB "$U" | tail -n 1)
HASH="${LINE#*:}"
case "$HASH" in '$2'*) ;; *) echo "Hashing failed." >&2; exit 1 ;; esac
kubectl -n ier create secret generic web-login --from-literal=WEB_USER="$U" --from-literal=WEB_HASH="$HASH" \
  --dry-run=client -o yaml | kubectl apply -f -
echo "Done. Secret web-login is in namespace ier."
