#!/usr/bin/env bash
# Run on server to deploy latest code.
set -euo pipefail

cd /opt/hireloop/app
git pull origin main

echo "Syntax check..."
sudo docker compose run --rm --no-deps bot bash -c \
  "python -m compileall -q bot/ ai/ db/ jobs/ resume/ && echo 'OK'" \
  || { echo "Syntax errors found — aborting deploy"; exit 1; }

sudo docker compose run --rm --no-deps bot bash -c "alembic upgrade head"
sudo docker compose up -d --build bot
sudo docker compose logs --tail=20 bot
