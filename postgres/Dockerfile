FROM postgres:18-alpine

ENV POSTGRES_USER=agentiq
ENV POSTGRES_PASSWORD=agentiq_secret
ENV POSTGRES_DB=agentiqprod

# provision.sql runs automatically on first start (empty data dir).
# Creates the agentiq role, all 26 tables, indexes, seeds reference data,
# and stamps alembic_version to head 0015 — idempotent.
COPY backend/database/provision/provision.sql /docker-entrypoint-initdb.d/01_provision.sql

EXPOSE 5432
