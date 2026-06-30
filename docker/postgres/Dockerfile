# ─────────────────────────────────────────────────────────────────────────────
# AgentIQ Postgres
# Extends postgres:16-alpine with AgentIQ-specific defaults.
# Application tables are created by Alembic migrations (run on backend startup).
# ─────────────────────────────────────────────────────────────────────────────
#
# Build:  docker build -t agentiq-postgres:1.0 .
# Run:    docker run -e POSTGRES_PASSWORD=secret -p 5432:5432 agentiq-postgres:1.0
# ─────────────────────────────────────────────────────────────────────────────

FROM postgres:16-alpine

ENV POSTGRES_DB=agentiq
ENV POSTGRES_USER=agentiq
# POSTGRES_PASSWORD must be supplied at runtime via env / docker-compose

# Init script — runs once when the data volume is empty
COPY init.sql /docker-entrypoint-initdb.d/01_init.sql

EXPOSE 5432

HEALTHCHECK --interval=10s --timeout=5s --start-period=30s --retries=5 \
  CMD pg_isready -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" || exit 1
