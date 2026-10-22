#!/usr/bin/env bash
# Deletes uploads, transcripts and job folders older than RETENTION_DAYS.
# Installed as a daily cron job by deploy/setup-ec2.sh. Without it the disk
# fills up: every source video and every output video is kept forever.
set -euo pipefail

DATA_DIR="${DATA_DIR:?DATA_DIR must be set}"
RETENTION_DAYS="${RETENTION_DAYS:-14}"

echo "$(date -Is) pruning files older than ${RETENTION_DAYS} days in ${DATA_DIR}"

for sub in videos transcripts; do
  if [[ -d "$DATA_DIR/$sub" ]]; then
    find "$DATA_DIR/$sub" -type f ! -name .gitkeep -mtime +"$RETENTION_DAYS" -print -delete
  fi
done

if [[ -d "$DATA_DIR/jobs" ]]; then
  # A job folder's mtime changes whenever job.json is rewritten, so a running
  # job is never older than the retention window.
  find "$DATA_DIR/jobs" -mindepth 1 -maxdepth 1 -type d -mtime +"$RETENTION_DAYS" \
    -print -exec rm -rf {} +
fi
