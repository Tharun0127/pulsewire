#!/bin/sh
set -e

if [ "${RUN_MIGRATIONS:-false}" = "true" ]; then
  echo "[entrypoint] ${INSTANCE_ID}: running migrations and seed"
  alembic upgrade head
  python -m scripts.seed
fi

# ONE worker per instance on purpose: the in-memory connection registry is
# then exactly "the sockets on this server", which is what api-1 vs api-2
# means in the demo. Scaling = more instances, not more workers.
echo "[entrypoint] ${INSTANCE_ID}: starting uvicorn"
exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1 --no-access-log
