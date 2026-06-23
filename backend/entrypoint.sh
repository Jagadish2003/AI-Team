#!/bin/sh
set -e

if [ -z "${DATABASE_URL:-}" ]; then
    echo "[entrypoint] ERROR: DATABASE_URL is not set."
    exit 1
fi

MASKED=$(printf '%s' "$DATABASE_URL" | sed 's|.*@||; s|\?.*||')
echo "[entrypoint] DATABASE_URL -> $MASKED"

echo "[entrypoint] Running database migrations..."
alembic upgrade head

echo "[entrypoint] Seeding reference data..."
python database/seed_loader.py

echo "[entrypoint] Starting AgentIQ backend on port 8000..."
exec python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
