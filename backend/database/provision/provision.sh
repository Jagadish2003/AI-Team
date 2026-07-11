#!/usr/bin/env bash
# Provision the complete AgentIQ schema onto a target PostgreSQL database
# (Alembic migrations + core {id,payload} tables + lazy-only tables + seed).
#
# Maintained provisioning path. Idempotent and safe to re-run. Assumes the role
# and database already exist (the database must be pre-created; the agentiq role
# is created by provision.sql in the pure-SQL path).
#
# DATABASE_URL is resolved in this order:
#   1. an exported DATABASE_URL environment variable (wins), else
#   2. the DATABASE_URL line in backend/.env.
#
# Usage:
#   ./provision.sh                                                   # uses backend/.env
#   DATABASE_URL=postgresql://agentiq:secret@db-host:5432/agentiq ./provision.sh
#   ./provision.sh --no-seed
#   ./provision.sh --reset          # DESTRUCTIVE: drop every table, then rebuild
#   ./provision.sh --reset --yes    # same, non-interactive (skips the typed confirm)
#
# By default this is idempotent and drops NOTHING. --reset drops the whole public
# schema first (IRREVERSIBLE) and requires typing the target database name to
# confirm — it is never implied, so a plain run can never wipe data by accident.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# backend/ is two levels up (provision -> database -> backend).
BACKEND_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"

# Fall back to backend/.env when DATABASE_URL is not already exported. Reads the
# last DATABASE_URL=... line, strips an optional surrounding "" or '' quote, and
# ignores commented (#) lines. An exported value still takes precedence.
if [ -z "${DATABASE_URL:-}" ] && [ -f "${BACKEND_DIR}/.env" ]; then
    DATABASE_URL="$(
        grep -E '^[[:space:]]*DATABASE_URL[[:space:]]*=' "${BACKEND_DIR}/.env" \
            | grep -vE '^[[:space:]]*#' \
            | tail -n1 \
            | sed -E 's/^[[:space:]]*DATABASE_URL[[:space:]]*=[[:space:]]*//; s/^"(.*)"$/\1/; s/^'\''(.*)'\''$/\1/'
    )"
    [ -n "${DATABASE_URL}" ] && export DATABASE_URL
fi

if [ -z "${DATABASE_URL:-}" ]; then
    echo "ERROR: DATABASE_URL is not set and was not found in ${BACKEND_DIR}/.env" >&2
    echo "  export DATABASE_URL=postgresql://agentiq:secret@db-host:5432/agentiq" >&2
    echo "  (or add a DATABASE_URL=... line to backend/.env)" >&2
    exit 1
fi

cd "${BACKEND_DIR}"
exec python database/provision/provision_schema.py "$@"
