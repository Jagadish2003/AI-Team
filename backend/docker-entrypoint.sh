#!/bin/bash
set -e

# Initialize SQLite database on first run
if [ ! -f /app/database/dev.db ]; then
    echo "[agentiq] First run — seeding database..."
    python database/seed_loader.py
fi

# Run Alembic migrations when PostgreSQL DATABASE_URL is configured
if [ -n "$DATABASE_URL" ] && [[ "$DATABASE_URL" == postgresql* ]]; then
    echo "[agentiq] Running database migrations..."
    alembic upgrade head || echo "[agentiq] WARNING: migrations failed — continuing"
fi

exec python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
