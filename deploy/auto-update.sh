#!/usr/bin/env bash
# Pull the latest main and rebuild, waiting for running jobs to finish first.
#
# GitHub Actions runs this after the tests pass on a push to main (see
# .github/workflows/tests.yml). Its SSH key is locked to this script in
# ~/.ssh/authorized_keys, so it can't open a shell. It also works by hand:
#   bash deploy/auto-update.sh
#
# Settings: WAIT_MINUTES (default 45) is how long to wait for busy jobs.
set -euo pipefail

# Everything runs inside main(): bash reads the whole function before it
# starts, so "git reset" replacing this file mid-run can't break it.
main() {
  local repo_dir data_dir wait_minutes log
  repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
  data_dir="${DATA_DIR:-$repo_dir/data}"
  wait_minutes="${WAIT_MINUTES:-45}"
  log="$repo_dir/auto-update.log"
  cd "$repo_dir"

  # One update at a time.
  exec 9>"$repo_dir/.auto-update.lock"
  if ! flock -n 9; then
    echo "Another update is already running." >&2
    exit 1
  fi

  exec > >(tee -a "$log") 2>&1
  echo "=== $(date -u +%FT%TZ) update requested"

  # While this file is there the autopilot starts no new job or upload (it
  # ignores one older than 2 h), so nothing begins that the restart would cut
  # off. It goes away however this script ends.
  deploy_flag="$data_dir/state/.deploying"
  if mkdir -p "$data_dir/state" && date -u +%FT%TZ > "$deploy_flag"; then
    trap 'rm -f "$deploy_flag"' EXIT
  else
    echo "Could not write $deploy_flag; the autopilot may start work during the update."
  fi

  # A restart kills any meeting being cut or uploaded, so wait for them.
  local waited=0
  while true; do
    local busy
    busy="$(busy_jobs "$data_dir/jobs")"
    [[ "$busy" == "0" ]] && break
    if (( waited >= wait_minutes )); then
      echo "Still $busy job(s) running after $wait_minutes min. Not updating."
      echo "Update by hand later: bash deploy/auto-update.sh"
      exit 1
    fi
    echo "$busy job(s) running, waiting (${waited}/${wait_minutes} min)"
    sleep 60
    waited=$((waited + 1))
  done

  echo "==> Pulling main"
  local before after
  before="$(git rev-parse --short HEAD)"
  git fetch --quiet origin main
  # The server never has its own code changes; .env, deploy/caddy.env and
  # data/ are ignored by git, so this keeps them.
  git reset --quiet --hard origin/main
  after="$(git rev-parse --short HEAD)"
  echo "$before -> $after"

  echo "==> Rebuilding"
  docker compose up -d --build --remove-orphans
  # Caddy reads its config from a mounted file and compose doesn't notice a
  # changed file: recreate Caddy whenever the Caddyfile isn't the one it was
  # last started with (remembered in .caddyfile.sha, ignored by git).
  local caddy_sum
  caddy_sum="$(sha256sum deploy/Caddyfile | cut -d' ' -f1)"
  if [[ "$(cat .caddyfile.sha 2>/dev/null || true)" != "$caddy_sum" ]]; then
    echo "==> The Caddyfile changed: recreating Caddy"
    docker compose up -d --force-recreate --no-deps caddy
    echo "$caddy_sum" > .caddyfile.sha
  fi

  echo "==> Waiting for the app to be healthy"
  local i
  for i in $(seq 1 60); do
    if docker compose ps app 2>/dev/null | grep -q "(healthy)"; then
      echo "Healthy after ~$((i * 5))s"
      docker image prune -f >/dev/null
      echo "=== done: $after"
      return 0
    fi
    sleep 5
  done
  echo "The app didn't become healthy within 5 minutes. Recent logs:"
  docker compose logs --tail 40 app
  exit 1
}

# Prints how many jobs are still being processed or uploaded to YouTube (the
# full video or any of its shorts). A job is done with once it reaches one of
# the `finished` statuses (core/models.py TERMINAL_STATUSES; a test keeps the
# two lists equal), so a stage added later is waited for without editing this.
busy_jobs() {
  python3 - "$1" <<'PY'
import json, pathlib, sys
finished = {"done", "failed", "skipped_desync", "cancelled", "decided"}
uploading = {"queued", "uploading"}
count = 0
for path in pathlib.Path(sys.argv[1]).glob("*/job.json"):
    try:
        job = json.loads(path.read_text())
    except (OSError, ValueError):
        continue
    shorts = job.get("youtube_shorts") or {}
    busy_upload = job.get("youtube_status") in uploading or any(
        isinstance(s, dict) and s.get("status") in uploading for s in shorts.values()
    )
    status = job.get("status")
    if (status and status not in finished) or busy_upload:
        count += 1
print(count)
PY
}

main "$@"
exit
