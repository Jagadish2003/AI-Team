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
echo "[entrypoint] Starting AgentIQ backend on port 8000..."
exec python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
