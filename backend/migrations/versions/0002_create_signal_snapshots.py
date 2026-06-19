"""Create signal_snapshots table with indexes for temporal baselining.

T3-S10-A — temporal signal storage.  One row per detector metric per run.
Schema is locked after T3-S10-A merges — column names and types must not
change without updating all downstream baseline queries.

Column types use the exact strings asserted by test_signal_snapshots_schema.py:
  VARCHAR(n)  — character strings
  DOUBLE      — floating-point metrics
  BOOLEAN     — fired flag
  TIMESTAMP   — datetime columns (stored as ISO-8601 text in SQLite)

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-29

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # Table — exact column types required by test_signal_snapshots_schema.py
    # ------------------------------------------------------------------
    op.execute("""
        CREATE TABLE IF NOT EXISTS signal_snapshots (
            id                   VARCHAR(36)  NOT NULL PRIMARY KEY,
            org_id               VARCHAR(64)  NOT NULL,
            run_id               VARCHAR(64)  NOT NULL,
            pack_id              VARCHAR(64)  NOT NULL,
            detector_id          VARCHAR(128) NOT NULL,
            signal_key           VARCHAR(256) NOT NULL,
            metric_name          VARCHAR(128) NOT NULL,
            metric_value         DOUBLE PRECISION NOT NULL,
            threshold            DOUBLE PRECISION,
            fired                BOOLEAN      NOT NULL,
            signal_source        VARCHAR(64)  NOT NULL,
            captured_at          TIMESTAMP    NOT NULL,
            baseline_mean        DOUBLE PRECISION,
            baseline_stddev      DOUBLE PRECISION,
            baseline_window_days INTEGER,
            baseline_calculated_at TIMESTAMP
        )
    """)

    # ------------------------------------------------------------------
    # Indexes — captured_at DESC on time-range indexes for query plan
    # efficiency (asserted by test_signal_snapshots_schema.py)
    # ------------------------------------------------------------------
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_ss_org_signal_time
            ON signal_snapshots (org_id, signal_key, captured_at DESC)
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_ss_org_run
            ON signal_snapshots (org_id, run_id)
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_ss_org_detector
            ON signal_snapshots (org_id, detector_id, captured_at DESC)
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_ss_baseline_stale
            ON signal_snapshots (baseline_calculated_at)
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_ss_baseline_stale")
    op.execute("DROP INDEX IF EXISTS idx_ss_org_detector")
    op.execute("DROP INDEX IF EXISTS idx_ss_org_run")
    op.execute("DROP INDEX IF EXISTS idx_ss_org_signal_time")
    op.execute("DROP TABLE IF EXISTS signal_snapshots")
