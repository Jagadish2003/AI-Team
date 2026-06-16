-- =============================================================================
-- AgentIQ — PostgreSQL bootstrap (role + database)
-- =============================================================================
-- Run ONCE per dedicated server, as a PostgreSQL SUPERUSER (e.g. `postgres`),
-- connected to the maintenance database `postgres`:
--
--     psql -h <DB_HOST> -p 5432 -U postgres -d postgres -f 00_create_role_and_db.sql
--
-- This creates the application role and the `agentiq` database only. It does
-- NOT create any tables — that is done afterwards by either:
--   (A) the Alembic + seed runbook  (provision.sh / provision.ps1), or
--   (B) the pure-SQL bundle         (01_schema.sql then 02_seed.sql).
--
-- CHANGE THE PASSWORD below before running in any shared/production server.
-- The application connects with:
--     DATABASE_URL=postgresql://agentiq:<password>@<DB_HOST>:5432/agentiq
-- =============================================================================

-- 1. Application role. Login role, no superuser, cannot create databases.
--    CREATE ROLE has no IF NOT EXISTS; the DO block makes it idempotent.
DO
$$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'agentiq') THEN
        CREATE ROLE agentiq LOGIN PASSWORD 'change-me-in-production';
    END IF;
END
$$;

-- 2. Application database, owned by the agentiq role.
--    CREATE DATABASE cannot run inside a transaction/DO block, and has no
--    IF NOT EXISTS, so it is guarded with \gexec (skipped if the DB exists).
SELECT 'CREATE DATABASE agentiq OWNER agentiq'
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'agentiq')
\gexec

-- 3. Make agentiq the owner of (and full privilege holder on) the public schema
--    in the new database, so Alembic / seed scripts can create objects.
\connect agentiq
ALTER SCHEMA public OWNER TO agentiq;
GRANT ALL ON SCHEMA public TO agentiq;
