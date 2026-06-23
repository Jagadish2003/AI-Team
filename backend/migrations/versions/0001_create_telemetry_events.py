"""Create telemetry_events table, indexes, and append-only enforcement.

Wide schema — new event types add payload fields in JSON without further
schema migrations.  Append-only enforcement (AT-288 / Fix 1, F1-AC5):
  - PostgreSQL ON UPDATE / ON DELETE DO INSTEAD NOTHING rules (this migration).
    Rules silently discard UPDATE/DELETE attempts, keeping the table append-only
    at the DB layer without SQLite-specific BEFORE UPDATE/DELETE triggers.

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
    # Append-only enforcement — PostgreSQL DO INSTEAD NOTHING rules.
    #
    # These replace the SQLite BEFORE UPDATE / BEFORE DELETE triggers. Any
    # UPDATE or DELETE against telemetry_events is silently discarded, keeping
    # the table append-only at the DB layer. CREATE OR REPLACE makes this
    # idempotent (the runtime _ensure_telemetry_table() applies the same DDL).
    # ------------------------------------------------------------------
    op.execute(
        "CREATE OR REPLACE RULE trg_telemetry_no_update AS "
        "ON UPDATE TO telemetry_events DO INSTEAD NOTHING"
    )
    op.execute(
        "CREATE OR REPLACE RULE trg_telemetry_no_delete AS "
        "ON DELETE TO telemetry_events DO INSTEAD NOTHING"
    )


def downgrade() -> None:
    op.execute("DROP RULE IF EXISTS trg_telemetry_no_delete ON telemetry_events")
    op.execute("DROP RULE IF EXISTS trg_telemetry_no_update ON telemetry_events")
    op.execute("DROP INDEX IF EXISTS idx_telemetry_org_run")
    op.execute("DROP INDEX IF EXISTS idx_telemetry_org_event")
    op.execute("DROP INDEX IF EXISTS idx_telemetry_org_ts")
    op.execute("DROP TABLE IF EXISTS telemetry_events")
