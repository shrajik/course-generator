#!/usr/bin/env bash
# One-time setup of a fresh Ubuntu server for the GitHub Actions deploy. Run as root:
#
#   bash server-setup.sh "ssh-ed25519 AAAA... github-actions-deploy"
#
# The argument is the PUBLIC half of the deploy key. Safe to re-run.
set -euo pipefail

PUBKEY="${1:-}"
if [[ -z "$PUBKEY" || "$PUBKEY" != ssh-* ]]; then
  echo "usage: bash server-setup.sh \"<public key line starting with ssh-ed25519>\"" >&2
  exit 1
fi
if [[ "$(id -u)" -ne 0 ]]; then echo "run as root" >&2; exit 1; fi

export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y rsync ufw curl ca-certificates

if ! command -v docker >/dev/null 2>&1; then
  curl -fsSL https://get.docker.com | sh
fi
docker compose version >/dev/null

# Deploy user: in the docker group (which is effectively root), used only for deployments.
id deploy >/dev/null 2>&1 || adduser --disabled-password --gecos "" deploy
usermod -aG docker deploy
install -d -m 700 -o deploy -g deploy /home/deploy/.ssh
touch /home/deploy/.ssh/authorized_keys
grep -qxF "$PUBKEY" /home/deploy/.ssh/authorized_keys || echo "$PUBKEY" >> /home/deploy/.ssh/authorized_keys
chmod 600 /home/deploy/.ssh/authorized_keys
chown deploy:deploy /home/deploy/.ssh/authorized_keys
install -d -o deploy -g deploy /opt/course-generator

# Firewall: SSH, HTTP, HTTPS. Allow SSH first so enabling it cannot lock you out.
ufw allow 22/tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw allow 443/udp
ufw --force enable

echo
echo "Done. Verify from your PC:  ssh -i <deploy_key> deploy@$(hostname -I | awk '{print $1}') 'docker ps'"
