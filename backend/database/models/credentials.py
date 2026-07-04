# DDL for the credentials table.
#
# No ORM — this project uses raw sqlite3. This module defines the table SQL so
# vault.py and tests can share a single source of truth for the schema.
#
# Migration strategy: vault._init_credentials_table() calls CREATE TABLE IF NOT EXISTS
# on first use, matching the pattern used by db._init_kv_table() and db.init_tables().
# ALTER_CREDENTIALS_ADD_REFRESH_FAILED is applied separately (idempotent try/except)
# to handle databases created before the refresh_failed column was added.

CREATE_CREDENTIALS_TABLE = """
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
    is_deleted      BOOLEAN NOT NULL DEFAULT FALSE,
    kind            TEXT NOT NULL DEFAULT 'oauth',
    enc_username    TEXT,
    enc_secret      TEXT,
    base_url        TEXT,
    UNIQUE(org_id, connector_id)
)
"""

# Applied for databases that pre-date the soft-delete column (idempotent).
ALTER_CREDENTIALS_ADD_IS_DELETED = (
    "ALTER TABLE credentials ADD COLUMN IF NOT EXISTS is_deleted BOOLEAN NOT NULL DEFAULT FALSE"
)

# Applied for databases that pre-date this column. PostgreSQL supports
# ADD COLUMN IF NOT EXISTS, so this is idempotent without relying on catching a
# DuplicateColumn error (which would otherwise abort the transaction).
ALTER_CREDENTIALS_ADD_REFRESH_FAILED = (
    "ALTER TABLE credentials ADD COLUMN IF NOT EXISTS refresh_failed INTEGER NOT NULL DEFAULT 0"
)

# R17-D3 Addendum A (T10): second vault record type — static (non-OAuth)
# credentials. `kind` discriminates 'oauth' (existing token rows, backfilled by
# the DEFAULT) from 'static' (Jira API token, ServiceNow user/password, native
# DB connection credentials). enc_username/enc_secret are Fernet-encrypted like
# access_token/refresh_token; base_url is non-secret instance location.
# Applied for databases that pre-date these columns (idempotent).
ALTER_CREDENTIALS_ADD_STATIC_CREDENTIAL_COLUMNS = (
    "ALTER TABLE credentials "
    "ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'oauth', "
    "ADD COLUMN IF NOT EXISTS enc_username TEXT, "
    "ADD COLUMN IF NOT EXISTS enc_secret TEXT, "
    "ADD COLUMN IF NOT EXISTS base_url TEXT"
)

CREATE_CREDENTIALS_IDX_ORG = (
    "CREATE INDEX IF NOT EXISTS idx_credentials_org_id ON credentials(org_id)"
)

CREATE_CREDENTIALS_IDX_CONNECTOR = (
    "CREATE INDEX IF NOT EXISTS idx_credentials_connector_id ON credentials(connector_id)"
)
