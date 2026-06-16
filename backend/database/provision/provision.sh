#!/usr/bin/env bash
# Provision the complete AgentIQ schema onto a target PostgreSQL database
# (Alembic migrations + core {id,payload} tables + lazy-only tables + seed).
#
# Maintained provisioning path. Idempotent and safe to re-run. Assumes the role
# and database already exist (run 00_create_role_and_db.sql once as a superuser
# first).
#
# Usage:
#   DATABASE_URL=postgresql://agentiq:secret@db-host:5432/agentiq ./provision.sh
#   ./provision.sh --no-seed
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# backend/ is two levels up (provision -> database -> backend).
BACKEND_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"

if [ -z "${DATABASE_URL:-}" ]; then
    echo "ERROR: DATABASE_URL is not set." >&2
    echo "  export DATABASE_URL=postgresql://agentiq:secret@db-host:5432/agentiq" >&2
    exit 1
fi

cd "${BACKEND_DIR}"
exec python database/provision/provision_schema.py "$@"
