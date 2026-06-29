#!/bin/sh
set -e
python manage.py collectstatic --no-input --clear
python manage.py migrate
# Start periodic jobs (api/crontab) then exec the main process (uvicorn).
if [ -f /app/crontab ]; then
    supercronic /app/crontab &
fi
exec "$@"
