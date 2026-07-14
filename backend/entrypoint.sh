#!/bin/sh
set -e

# Accept either DATABASE_URL (explicit) or PROD_DATABASE_URL (loaded from env_file).
# This makes the container work whether started via `bash start.sh` (which passes
# DATABASE_URL through compose interpolation) or via a manual `docker compose up`.
DATABASE_URL="${DATABASE_URL:-${PROD_DATABASE_URL:-}}"
export DATABASE_URL

if [ -z "$DATABASE_URL" ]; then
    echo "[entrypoint] ERROR: DATABASE_URL is not set."
    echo "[entrypoint]        Set DATABASE_URL or PROD_DATABASE_URL in the .env file."
    exit 1
fi

MASKED=$(printf '%s' "$DATABASE_URL" | sed 's|.*@||; s|\?.*||')
echo "[entrypoint] DATABASE_URL -> $MASKED"

# Bring an existing database up to the code's schema before serving.
# provision.sql only runs on a FIRST start with an empty data directory, so a
# persisted database from an older image would otherwise stay on the old
# schema forever (e.g. orgs.name_normalised missing -> 500 on /api/auth/register).
# alembic is a no-op when the schema is already at head.
echo "[entrypoint] Applying database migrations (alembic upgrade head)..."
alembic upgrade head
echo "[entrypoint] Migrations up to date."

echo "[entrypoint] Starting AgentIQ backend on port 8000..."
exec python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
