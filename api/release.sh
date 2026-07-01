#!/bin/sh
set -e
python manage.py collectstatic --no-input --clear
python manage.py migrate
# Start periodic jobs (api/crontab) then exec the main process (uvicorn).
# All services share this image/entrypoint (api, worker-heavy, worker-light,
# worker-bulk) — gate on CRON_ENABLED so supercronic only runs once, on the
# api service, instead of once per container (which was silently 4x-firing
# every scheduled task).
if [ "$CRON_ENABLED" = "true" ] && [ -f /app/crontab ]; then
    supercronic /app/crontab &
fi
exec "$@"
