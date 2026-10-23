#!/usr/bin/env bash
# One-time server setup for Ubuntu 24.04 on EC2. Run as the default ubuntu
# user from inside the cloned repo:
#   sudo bash deploy/setup-ec2.sh
#
# Installs Docker, adds swap if the machine has less than 8 GB RAM, prepares
# the data folder, schedules the daily cleanup job, locks SSH to keys only
# (with fail2ban) and turns on security updates. Safe to re-run.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_DIR="${DATA_DIR:-$REPO_DIR/data}"
RETENTION_DAYS="${RETENTION_DAYS:-3}"
SWAP_SIZE_GB="${SWAP_SIZE_GB:-4}"

if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo: sudo bash deploy/setup-ec2.sh" >&2
  exit 1
fi

echo "==> Installing Docker"
if ! command -v docker >/dev/null 2>&1; then
  apt-get update
  apt-get install -y ca-certificates curl
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  # shellcheck disable=SC1091
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi
systemctl enable --now docker
if [[ -n "${SUDO_USER:-}" ]]; then
  usermod -aG docker "$SUDO_USER"
fi

echo "==> Swap"
MEM_MB=$(awk '/MemTotal/ {printf "%d", $2/1024}' /proc/meminfo)
MEM_GB=$(( (MEM_MB + 512) / 1024 ))
# An "8 GB" instance reports ~7.6 GB, hence the 7000 MB threshold.
if [[ "$MEM_MB" -lt 7000 ]] && ! swapon --show | grep -q /swapfile; then
  fallocate -l "${SWAP_SIZE_GB}G" /swapfile
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  sysctl -w vm.swappiness=10
  echo 'vm.swappiness=10' > /etc/sysctl.d/99-swappiness.conf
  echo "    added ${SWAP_SIZE_GB} GB swap (RAM is ${MEM_GB} GB)"
else
  echo "    skipped (RAM ${MEM_GB} GB, or swap already present)"
fi

echo "==> Data folder"
mkdir -p "$DATA_DIR"
# The container runs as uid 1000 ("app"), so it must own the data folder.
chown -R 1000:1000 "$DATA_DIR"

echo "==> Daily cleanup of jobs older than ${RETENTION_DAYS} days"
chmod +x "$REPO_DIR/deploy/cleanup.sh"
cat > /etc/cron.d/highlight-cutter-cleanup <<EOF
# Prune old Highlight Cutter uploads and job outputs.
30 3 * * * root DATA_DIR=$DATA_DIR RETENTION_DAYS=$RETENTION_DAYS $REPO_DIR/deploy/cleanup.sh >> /var/log/highlight-cutter-cleanup.log 2>&1
EOF
chmod 644 /etc/cron.d/highlight-cutter-cleanup

echo "==> SSH: keys only, and ban IPs that keep guessing"
# Port 22 may be open to anywhere (each person gets their own key in
# ~/.ssh/authorized_keys), so passwords must never be accepted.
cat > /etc/ssh/sshd_config.d/00-keys-only.conf <<'EOF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
EOF
# Ubuntu 24.04 starts sshd on demand (ssh.socket), so the privilege
# separation dir may not exist yet and "sshd -t" would fail without it.
mkdir -p /run/sshd
sshd -t
systemctl try-reload-or-restart ssh
# Fresh package lists: the first fail2ban build for 24.04 crashed on Python 3.12.
apt-get update
apt-get install -y fail2ban
cat > /etc/fail2ban/jail.d/sshd.local <<'EOF'
[sshd]
enabled = true
maxretry = 5
findtime = 10m
bantime = 1h
EOF
systemctl enable fail2ban
systemctl restart fail2ban

echo "==> Automatic security updates"
apt-get install -y unattended-upgrades
dpkg-reconfigure -f noninteractive unattended-upgrades

echo
echo "Done. Log out and back in once so your user can run docker without sudo."
echo "Next: create .env and deploy/caddy.env, then: docker compose up -d --build"
