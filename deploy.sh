#!/bin/sh
# deploy.sh — pull latest code and rebuild changed containers.
#
# Usage:
#   ./deploy.sh              # deploy from current branch
#   ./deploy.sh master         # deploy a specific branch
#
# Called by: git post-receive hook, webhook listener, or manually.

set -e

BRANCH="${1:-master}"
COMPOSE="docker compose"

echo "==> deploy: pulling $BRANCH"
git fetch origin
git reset --hard "origin/$BRANCH"

echo "==> deploy: rebuilding changed images"
$COMPOSE build --pull --parallel

echo "==> deploy: restarting services"
# --no-deps: only restart the services whose images changed, not their deps
$COMPOSE up -d --remove-orphans

echo "==> deploy: cleaning up old images"
docker image prune -f

echo "==> deploy: done"
$COMPOSE ps
