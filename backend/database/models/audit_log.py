"""DDL for audit_log — AT-82 T3. SQLite-compatible (TEXT replaces TIMESTAMPTZ/JSONB/UUID).

PostgreSQL deployment note — apply after CREATE TABLE:
    REVOKE UPDATE, DELETE ON audit_log FROM app_user;
    GRANT INSERT, SELECT ON audit_log TO app_user;
"""

CREATE_AUDIT_LOG_TABLE = """
CREATE TABLE IF NOT EXISTS audit_log (
    id           TEXT PRIMARY KEY,
    org_id       TEXT NOT NULL,
    event_type   TEXT NOT NULL,
    user_id      TEXT,
    run_id       TEXT,
    connector_id TEXT,
    payload      TEXT,
    timestamp    TEXT NOT NULL
)
"""

CREATE_AUDIT_LOG_IDX_ORG_TS = """
CREATE INDEX IF NOT EXISTS idx_audit_org_ts ON audit_log (org_id, timestamp)
"""

CREATE_AUDIT_LOG_IDX_ORG_EVENT = """
CREATE INDEX IF NOT EXISTS idx_audit_org_event ON audit_log (org_id, event_type)
"""
