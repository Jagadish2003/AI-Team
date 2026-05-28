"""Telemetry events table model. Append-only. See T1-S10-C.

SQLite-compatible DDL (TEXT replaces PostgreSQL-specific types):
  - UUID        → TEXT  (Python uuid.uuid4() generated at write time)
  - TIMESTAMPTZ → TEXT  (ISO-8601 UTC strings, consistent with all other tables)
  - JSONB       → TEXT  (JSON-serialised string)

PostgreSQL deployment note — execute after CREATE TABLE:
    REVOKE UPDATE, DELETE ON telemetry_events FROM <app_db_user>;
    GRANT INSERT, SELECT ON telemetry_events TO <app_db_user>;

Table is append-only: the application never issues UPDATE or DELETE against
telemetry_events.  This is enforced at the application layer (record_event()
in AT-89 C2 only INSERTs).  In a PostgreSQL deployment apply the REVOKE above
to enforce at the DB layer.

No Alembic in this project — table is created lazily on first use via
_ensure_telemetry_table() in the write API (AT-89 C2).  All other tables
(credentials, audit_log, workspace_members) follow the same pattern.
"""

# ---------------------------------------------------------------------------
# Table DDL
# ---------------------------------------------------------------------------

CREATE_TELEMETRY_EVENTS_TABLE = """
CREATE TABLE IF NOT EXISTS telemetry_events (
    id           TEXT PRIMARY KEY,
    org_id       TEXT NOT NULL,
    event_type   TEXT NOT NULL,
    source       TEXT NOT NULL,
    run_id       TEXT,
    connector_id TEXT,
    pack_id      TEXT,
    duration_ms  INTEGER,
    success      INTEGER,
    count        INTEGER,
    error_code   TEXT,
    payload      TEXT NOT NULL DEFAULT '{}',
    timestamp    TEXT NOT NULL
)
"""

# ---------------------------------------------------------------------------
# Composite indexes  (supersede single-column indexes per spec)
# ---------------------------------------------------------------------------

CREATE_TELEMETRY_IDX_ORG_TS = """
CREATE INDEX IF NOT EXISTS idx_telemetry_org_ts
    ON telemetry_events (org_id, timestamp)
"""

CREATE_TELEMETRY_IDX_ORG_EVENT = """
CREATE INDEX IF NOT EXISTS idx_telemetry_org_event
    ON telemetry_events (org_id, event_type)
"""

CREATE_TELEMETRY_IDX_ORG_RUN = """
CREATE INDEX IF NOT EXISTS idx_telemetry_org_run
    ON telemetry_events (org_id, run_id)
"""

# ---------------------------------------------------------------------------
# Convenience tuple — iterate to initialise all DDL in order
# ---------------------------------------------------------------------------

ALL_TELEMETRY_DDL: tuple[str, ...] = (
    CREATE_TELEMETRY_EVENTS_TABLE,
    CREATE_TELEMETRY_IDX_ORG_TS,
    CREATE_TELEMETRY_IDX_ORG_EVENT,
    CREATE_TELEMETRY_IDX_ORG_RUN,
)
