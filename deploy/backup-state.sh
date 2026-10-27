#!/usr/bin/env bash
# Copies the app's memory (data/state: the list of what is on YouTube, the
# upload journal, the autopilot's meetings and the Zoom account list) to an
# S3 bucket, one dated folder per day. Installed as a daily cron job by
# deploy/setup-ec2.sh; safe to run by hand: bash deploy/backup-state.sh
#
# The bucket comes from STATE_BACKUP_S3_URI in the repo's .env, e.g.
#   STATE_BACKUP_S3_URI=s3://flolabs-backups/highlight-cutter
# Permission comes from the EC2 machine's IAM role (s3:PutObject on that
# bucket), so no AWS keys are stored on the server. Without the setting it
# does nothing. The app also keeps a second copy on the server itself, in
# data/state-backup, after every change.
set -euo pipefail
# the snap AWS CLI lives in /snap/bin, which cron's PATH doesn't have
export PATH="$PATH:/snap/bin:/usr/local/bin"

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_DIR="${DATA_DIR:-$REPO_DIR/data}"

target="${STATE_BACKUP_S3_URI:-}"
if [[ -z "$target" && -f "$REPO_DIR/.env" ]]; then
  # read just this one line; never source the whole .env
  target="$(grep -E '^STATE_BACKUP_S3_URI=' "$REPO_DIR/.env" | tail -n 1 | cut -d= -f2- | tr -d "\"' \r" || true)"
fi
if [[ -z "$target" ]]; then
  echo "$(date -Is) STATE_BACKUP_S3_URI is not set; nothing to back up to."
  exit 0
fi
if [[ ! -d "$DATA_DIR/state" ]]; then
  echo "$(date -Is) $DATA_DIR/state does not exist yet; nothing to back up."
  exit 0
fi
if ! command -v aws >/dev/null 2>&1; then
  echo "$(date -Is) the AWS command line is missing: sudo snap install aws-cli --classic" >&2
  exit 1
fi

day="$(date -u +%F)"
echo "$(date -Is) copying $DATA_DIR/state to ${target%/}/$day/"
aws s3 cp --recursive --only-show-errors "$DATA_DIR/state" "${target%/}/$day/"
echo "$(date -Is) done"
