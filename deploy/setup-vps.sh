#!/usr/bin/env bash
# One-time setup for a fresh Ubuntu/Debian VPS. Run once as root (or via
# sudo) on a brand-new box before the first deploy.
#
# What this does, in order:
#   1. Basic OS updates
#   2. A non-root user to actually run deploys as (never deploy as root)
#   3. Docker + Docker Compose plugin
#   4. UFW firewall: only SSH, HTTP, HTTPS reach the box - the app itself
#      is never exposed directly (docker-compose.yml only `expose`s it
#      to Caddy on the internal network, not the host)
#   5. Fail2ban for basic SSH brute-force protection
#
# Usage:
#   scp deploy/setup-vps.sh root@your-vps-ip:~/
#   ssh root@your-vps-ip
#   bash setup-vps.sh
set -euo pipefail

DEPLOY_USER="${DEPLOY_USER:-nitocad}"

if [[ $EUID -ne 0 ]]; then
  echo "Run this as root (or with sudo)." >&2
  exit 1
fi

echo "==> Updating system packages"
apt-get update -y
apt-get upgrade -y

echo "==> Creating deploy user '${DEPLOY_USER}' (skip if it already exists)"
if ! id -u "${DEPLOY_USER}" >/dev/null 2>&1; then
  adduser --disabled-password --gecos "" "${DEPLOY_USER}"
  usermod -aG sudo "${DEPLOY_USER}"
  # Copy your SSH key over so you're not stuck on password auth for the
  # new user - assumes root's authorized_keys is already how you got in.
  mkdir -p "/home/${DEPLOY_USER}/.ssh"
  cp /root/.ssh/authorized_keys "/home/${DEPLOY_USER}/.ssh/authorized_keys" 2>/dev/null || \
    echo "    (no /root/.ssh/authorized_keys found - add one manually before disabling password auth)"
  chown -R "${DEPLOY_USER}:${DEPLOY_USER}" "/home/${DEPLOY_USER}/.ssh"
  chmod 700 "/home/${DEPLOY_USER}/.ssh"
  chmod 600 "/home/${DEPLOY_USER}/.ssh/authorized_keys" 2>/dev/null || true
fi

echo "==> Installing Docker + Compose plugin"
if ! command -v docker >/dev/null 2>&1; then
  curl -fsSL https://get.docker.com | sh
fi
usermod -aG docker "${DEPLOY_USER}"

echo "==> Configuring firewall (UFW): allow SSH, HTTP, HTTPS only"
apt-get install -y ufw fail2ban
ufw allow OpenSSH
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable

echo "==> Enabling fail2ban"
systemctl enable --now fail2ban

cat <<EOF

==> Done.

Next steps:
  1. Log out and back in as '${DEPLOY_USER}' (needed for the docker group
     membership to take effect):
       ssh ${DEPLOY_USER}@this-vps-ip

  2. Clone/copy the repo, then run deploy/deploy.sh from inside it - see
     DEPLOYMENT.md.

  3. Consider disabling root SSH login and password auth entirely once
     you've confirmed the '${DEPLOY_USER}' account works
     (/etc/ssh/sshd_config: PermitRootLogin no, PasswordAuthentication no,
     then 'systemctl restart sshd'). Not done automatically here so you
     can't lock yourself out mid-script.
EOF
