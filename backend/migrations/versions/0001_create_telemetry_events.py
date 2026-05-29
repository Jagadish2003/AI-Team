"""Create telemetry_events table, indexes, and append-only triggers.

Wide schema — new event types add payload fields in JSON without further
schema migrations.  Append-only enforcement:
  - SQLite:     BEFORE UPDATE / BEFORE DELETE triggers (this migration).
  - PostgreSQL: apply the REVOKE statements in the downgrade docstring.

Revision ID: 0001
Revises:
Create Date: 2026-05-29

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # Table
    # SQLite-compatible DDL.  For PostgreSQL deployment replace:
    #   TEXT (id, timestamps, JSON) -> UUID / TIMESTAMPTZ / JSONB
    #   INTEGER (success)           -> BOOLEAN
    # ------------------------------------------------------------------
    op.execute("""
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
    """)

    # ------------------------------------------------------------------
    # Composite indexes (cover the three most common query shapes)
    # ------------------------------------------------------------------
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_telemetry_org_ts
            ON telemetry_events (org_id, timestamp)
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_telemetry_org_event
            ON telemetry_events (org_id, event_type)
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_telemetry_org_run
            ON telemetry_events (org_id, run_id)
    """)

    # ------------------------------------------------------------------
    # Append-only enforcement — SQLite BEFORE UPDATE / DELETE triggers.
    #
    # PostgreSQL equivalent (run after table creation):
    #     REVOKE UPDATE, DELETE ON telemetry_events FROM <app_db_user>;
    #     GRANT  INSERT, SELECT  ON telemetry_events TO  <app_db_user>;
    # ------------------------------------------------------------------
    op.execute("""
        CREATE TRIGGER IF NOT EXISTS trg_telemetry_no_update
        BEFORE UPDATE ON telemetry_events
        BEGIN
            SELECT RAISE(ABORT, 'telemetry_events is append-only: UPDATE not permitted');
        END
    """)
    op.execute("""
        CREATE TRIGGER IF NOT EXISTS trg_telemetry_no_delete
        BEFORE DELETE ON telemetry_events
        BEGIN
            SELECT RAISE(ABORT, 'telemetry_events is append-only: DELETE not permitted');
        END
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_telemetry_no_delete")
    op.execute("DROP TRIGGER IF EXISTS trg_telemetry_no_update")
    op.execute("DROP INDEX IF EXISTS idx_telemetry_org_run")
    op.execute("DROP INDEX IF EXISTS idx_telemetry_org_event")
    op.execute("DROP INDEX IF EXISTS idx_telemetry_org_ts")
    op.execute("DROP TABLE IF EXISTS telemetry_events")
