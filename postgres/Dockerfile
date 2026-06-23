FROM postgres:18-alpine

# Default credentials — override at runtime via env vars or .env file
ENV POSTGRES_USER=agentiq
ENV POSTGRES_PASSWORD=agentiq_secret
ENV POSTGRES_DB=agentiqprod

# Tune postgres for a container workload
RUN echo "max_connections = 100"            >> /usr/local/share/postgresql/postgresql.conf.sample \
 && echo "shared_buffers = 128MB"           >> /usr/local/share/postgresql/postgresql.conf.sample \
 && echo "effective_cache_size = 256MB"     >> /usr/local/share/postgresql/postgresql.conf.sample \
 && echo "timezone = 'UTC'"                 >> /usr/local/share/postgresql/postgresql.conf.sample \
 && echo "log_timezone = 'UTC'"             >> /usr/local/share/postgresql/postgresql.conf.sample \
 && echo "log_min_duration_statement = 500" >> /usr/local/share/postgresql/postgresql.conf.sample

# provision.sql runs automatically on first start (empty data dir).
# Creates the agentiq role, all 26 tables, indexes, seeds reference data,
# and stamps alembic_version to head 0015 — all idempotent.
COPY backend/database/provision/provision.sql /docker-entrypoint-initdb.d/01_provision.sql

EXPOSE 5432
