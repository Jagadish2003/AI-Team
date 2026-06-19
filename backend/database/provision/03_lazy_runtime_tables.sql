-- =============================================================================
-- AgentIQ — formerly-lazy runtime tables
-- -----------------------------------------------------------------------------
-- credentials, nonces and oauth_nonces used to be created by the application at
-- runtime (vault.py / routes_connector_auth.py). That runtime DDL has been
-- removed: the application no longer creates tables. These three tables are NOT
-- in the Alembic migrations or seed_loader, so a database provisioned by
-- `alembic upgrade head` + seed_loader is missing them.
--
-- `provision_schema.py` / `provision.sh` already materialise these tables. Use
-- this script only to add them to an already-provisioned database that predates
-- the change (the dev/shared DB hit by CS-02).
--
-- Run as the schema owner (e.g. postgres), then grant to the application role:
--   psql -h <DB_HOST> -U postgres -d <DB_NAME> -v ON_ERROR_STOP=1 -f 03_lazy_runtime_tables.sql
-- =============================================================================

CREATE TABLE IF NOT EXISTS credentials (
    id              TEXT PRIMARY KEY,
    org_id          TEXT NOT NULL,
    connector_id    TEXT NOT NULL,
    access_token    TEXT NOT NULL,
    refresh_token   TEXT,
    expires_at      TEXT NOT NULL,
    scopes          TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    refresh_failed  INTEGER NOT NULL DEFAULT 0,
    UNIQUE(org_id, connector_id)
);
ALTER TABLE credentials ADD COLUMN IF NOT EXISTS refresh_failed INTEGER NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS idx_credentials_org_id ON credentials(org_id);
CREATE INDEX IF NOT EXISTS idx_credentials_connector_id ON credentials(connector_id);

CREATE TABLE IF NOT EXISTS nonces (
    key   TEXT PRIMARY KEY,
    data  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS oauth_nonces (
    nonce        TEXT PRIMARY KEY,
    connector_id TEXT NOT NULL,
    expires_at   TEXT NOT NULL
);

-- Grant DML to the application login role. Replace :app_role with the role in
-- DATABASE_URL (e.g. aiqdevusr), then uncomment:
-- GRANT SELECT, INSERT, UPDATE, DELETE ON credentials, nonces, oauth_nonces TO :app_role;
