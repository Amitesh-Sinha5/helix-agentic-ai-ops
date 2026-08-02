#!/usr/bin/env sh
# Applies migrations, then hands off to the container command.
#
# Running `alembic upgrade head` on start keeps a fresh `docker compose up` to a
# single command. It is idempotent, so restarts are safe -- but set
# RUN_MIGRATIONS=false and migrate as a separate step when running replicas, so
# several instances do not race to migrate the same database.
set -e

if [ "${RUN_MIGRATIONS:-true}" = "true" ]; then
  echo "[entrypoint] applying database migrations..."
  # Postgres may still be starting; retry rather than crash-loop the container.
  attempts=0
  until alembic upgrade head; do
    attempts=$((attempts + 1))
    if [ "$attempts" -ge 10 ]; then
      echo "[entrypoint] migrations failed after $attempts attempts" >&2
      exit 1
    fi
    echo "[entrypoint] migration attempt $attempts failed; retrying in 3s..."
    sleep 3
  done
  echo "[entrypoint] migrations applied."
else
  echo "[entrypoint] RUN_MIGRATIONS=false; skipping migrations."
fi

exec "$@"
