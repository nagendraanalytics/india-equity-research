#!/usr/bin/env bash
# One-time setup for the IER server (Ubuntu, Hetzner). Run as root, once:
#   bash bootstrap-server.sh
# Safe to re-run. Does NOT open any port except SSH (22).
set -euo pipefail

APP_USER="ier"
SWAP_GB=4

if [ "$(id -u)" -ne 0 ]; then
  echo "Run as root." >&2
  exit 1
fi

echo "==> 1/7 System updates"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get upgrade -y
apt-get install -y curl ca-certificates ufw fail2ban unattended-upgrades git

echo "==> 2/7 Automatic security updates"
dpkg-reconfigure -f noninteractive unattended-upgrades || true

echo "==> 3/7 User '${APP_USER}' with your SSH key"
if ! id "${APP_USER}" >/dev/null 2>&1; then
  adduser --disabled-password --gecos "" "${APP_USER}"
fi
usermod -aG sudo "${APP_USER}"
echo "${APP_USER} ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/90-${APP_USER}
chmod 440 /etc/sudoers.d/90-${APP_USER}
install -d -m 700 -o "${APP_USER}" -g "${APP_USER}" "/home/${APP_USER}/.ssh"
if [ ! -s /root/.ssh/authorized_keys ]; then
  echo "No SSH key found in /root/.ssh/authorized_keys. Stopping so you are not locked out." >&2
  exit 1
fi
cp /root/.ssh/authorized_keys "/home/${APP_USER}/.ssh/authorized_keys"
chown "${APP_USER}:${APP_USER}" "/home/${APP_USER}/.ssh/authorized_keys"
chmod 600 "/home/${APP_USER}/.ssh/authorized_keys"

echo "==> 4/7 SSH hardening (key-only, no root login)"
cat > /etc/ssh/sshd_config.d/99-ier.conf <<'EOF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
EOF
sshd -t
# Ubuntu 26.04 starts sshd per connection (socket activation), so the new
# config applies to the next login; a reload only works if ssh.service is running.
systemctl reload ssh 2>/dev/null || systemctl reload sshd 2>/dev/null || true

echo "==> 5/7 Swap (${SWAP_GB} GB) and firewall"
if ! swapon --show | grep -q /swapfile; then
  fallocate -l "${SWAP_GB}G" /swapfile
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi
sysctl -w vm.swappiness=20 >/dev/null
echo 'vm.swappiness=20' > /etc/sysctl.d/99-swappiness.conf

ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp
# k3s pod and service networks must reach the host (API server, DNS)
ufw allow from 10.42.0.0/16
ufw allow from 10.43.0.0/16
ufw --force enable
systemctl enable --now fail2ban

echo "==> 6/7 k3s (no ingress controller, API reachable only from this server)"
if ! command -v k3s >/dev/null 2>&1; then
  curl -sfL https://get.k3s.io | INSTALL_K3S_EXEC="server --disable traefik --disable servicelb --write-kubeconfig-mode 640 --write-kubeconfig /etc/rancher/k3s/k3s.yaml" sh -
fi
until k3s kubectl get nodes 2>/dev/null | grep -q ' Ready'; do
  echo "waiting for node..."; sleep 5
done
# let the app user run kubectl
install -d -o "${APP_USER}" -g "${APP_USER}" "/home/${APP_USER}/.kube"
cp /etc/rancher/k3s/k3s.yaml "/home/${APP_USER}/.kube/config"
chown "${APP_USER}:${APP_USER}" "/home/${APP_USER}/.kube/config"
chmod 600 "/home/${APP_USER}/.kube/config"
grep -q 'KUBECONFIG' "/home/${APP_USER}/.bashrc" || \
  echo 'export KUBECONFIG=$HOME/.kube/config' >> "/home/${APP_USER}/.bashrc"
ln -sf /usr/local/bin/k3s /usr/local/bin/kubectl 2>/dev/null || true

echo "==> 7/7 Argo CD (ClusterIP only, no public access)"
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
k3s kubectl create namespace argocd --dry-run=client -o yaml | k3s kubectl apply -f -
k3s kubectl apply -n argocd --server-side --force-conflicts \
  -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml
# save memory on a small server: these two parts are not needed
k3s kubectl -n argocd scale deploy argocd-dex-server --replicas=0 || true
k3s kubectl -n argocd scale deploy argocd-notifications-controller --replicas=0 || true
k3s kubectl -n argocd rollout status deploy/argocd-server --timeout=300s || true

cat <<EOF

Done. Next:
1. From your PC, open a NEW terminal and test:  ssh ${APP_USER}@<this-server-ip>
   Only after that works, close this root session.
2. Argo CD admin password (run as ${APP_USER} on the server):
   kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath="{.data.password}" | base64 -d
3. Argo CD UI from your PC (tunnel, nothing is public):
   ssh -L 8080:localhost:8080 ${APP_USER}@<this-server-ip> "kubectl -n argocd port-forward svc/argocd-server 8080:443"
   then open https://localhost:8080 (user: admin)
EOF
