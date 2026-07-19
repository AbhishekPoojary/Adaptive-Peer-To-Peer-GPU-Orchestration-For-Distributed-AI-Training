#!/bin/sh
# Runs Alembic migrations against the real configured database before
# starting the server. No migration steps are skipped or faked.
set -e

echo "[entrypoint] running alembic upgrade head..."
cd /app
alembic -c orchestrator/alembic.ini upgrade head

echo "[entrypoint] starting: $*"
exec "$@"
