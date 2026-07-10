#!/bin/sh
# Daily forced restart for the api + Celery worker containers — a backstop
# against slow memory growth (leaked/fragmented RSS that a process-level
# memory limit alone doesn't reclaim). Docker's memory limits on those
# services (docker-compose.yml) stop a runaway container before it can take
# the whole host down; this script complements that by periodically
# recycling every long-lived process regardless of its current RSS, so a
# slow leak never gets the chance to reach the limit in the first place.
#
# Runs as its own sidecar container (docker-compose.yml: `restarter`) with
# the host's Docker socket mounted read-only-in-spirit (docker restart still
# needs write access to the socket) so it can restart sibling containers by
# their compose service label — no host cron/systemd access required.
set -eu

RESTART_HOUR_UTC="${RESTART_HOUR_UTC:-4}"   # 04:00 UTC — low-traffic default
SERVICES="${RESTART_SERVICES:-api worker-heavy worker-light worker-bulk}"

log() {
  echo "[restarter] $(date -u +%FT%TZ) $*"
}

# Seconds from now until the next occurrence of RESTART_HOUR_UTC:00:00 UTC.
# Pure POSIX arithmetic (no GNU/BSD `date -d`/-j` extensions) so this runs
# unmodified on busybox/alpine.
seconds_until_next_run() {
  cur_h=$(date -u +%H); cur_m=$(date -u +%M); cur_s=$(date -u +%S)
  cur_secs_today=$((10#$cur_h * 3600 + 10#$cur_m * 60 + 10#$cur_s))
  target_secs=$((RESTART_HOUR_UTC * 3600))
  if [ "$cur_secs_today" -lt "$target_secs" ]; then
    echo $((target_secs - cur_secs_today))
  else
    echo $((86400 - cur_secs_today + target_secs))
  fi
}

restart_services() {
  log "restarting: $SERVICES"
  for svc in $SERVICES; do
    cid=$(docker ps -q --filter "label=com.docker.compose.service=$svc")
    if [ -n "$cid" ]; then
      # Celery workers ack late (CELERY_TASK_ACKS_LATE=True), so a task
      # in flight when a worker container is restarted gets redelivered
      # to another worker instead of lost — stagger restarts so the
      # whole pipeline never has zero workers on a queue at once.
      docker restart "$cid"
      sleep 10
    else
      log "WARNING: no running container found for service '$svc' — skipped"
    fi
  done
  log "done"
}

log "started — will restart {$SERVICES} daily at ${RESTART_HOUR_UTC}:00 UTC"
while true; do
  sleep "$(seconds_until_next_run)"
  restart_services
  sleep 60  # clear the target minute so we don't double-fire on drift
done
