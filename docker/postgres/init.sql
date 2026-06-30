-- AgentIQ PostgreSQL initialization
-- The application database and user are created automatically via the
-- POSTGRES_DB / POSTGRES_USER / POSTGRES_PASSWORD environment variables
-- set in the Dockerfile / docker-compose.
--
-- This script runs once on first container start (empty data volume).
-- Application tables are created by Alembic migrations run on backend startup.

-- Enable UUID generation (used by some ORM models)
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- Confirm
DO $$
BEGIN
  RAISE NOTICE 'AgentIQ database initialized.';
END
$$;
