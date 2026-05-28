"""Create signal_snapshots table.

Revision ID: 0001
Revises:
Create Date: 2026-05-27
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create the locked T3-S10-A signal_snapshots schema."""
    op.create_table(
        "signal_snapshots",
        sa.Column(
            "id",
            sa.UUID().with_variant(sa.String(36), "sqlite"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("org_id", sa.String(64), nullable=False),
        sa.Column("run_id", sa.String(64), nullable=False),
        sa.Column("pack_id", sa.String(64), nullable=False),
        sa.Column("detector_id", sa.String(128), nullable=False),
        sa.Column("signal_key", sa.String(256), nullable=False),
        sa.Column("metric_name", sa.String(128), nullable=False),
        sa.Column("metric_value", sa.Double(), nullable=False),
        sa.Column("threshold", sa.Double(), nullable=True),
        sa.Column("fired", sa.Boolean(), nullable=False),
        sa.Column("signal_source", sa.String(64), nullable=False),
        sa.Column("captured_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("baseline_mean", sa.Double(), nullable=True),
        sa.Column("baseline_stddev", sa.Double(), nullable=True),
        sa.Column("baseline_window_days", sa.Integer(), nullable=True),
        sa.Column("baseline_calculated_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )

    op.create_index(
        "idx_ss_org_signal_time",
        "signal_snapshots",
        ["org_id", "signal_key", sa.text("captured_at DESC")],
    )
    op.create_index(
        "idx_ss_org_run",
        "signal_snapshots",
        ["org_id", "run_id"],
    )
    op.create_index(
        "idx_ss_org_detector",
        "signal_snapshots",
        ["org_id", "detector_id", sa.text("captured_at DESC")],
    )
    if op.get_bind().dialect.name == "sqlite":
        # SQLite ascending indexes already order NULL values first.
        op.create_index(
            "idx_ss_baseline_stale",
            "signal_snapshots",
            ["baseline_calculated_at"],
        )
    else:
        op.execute(
            """
            CREATE INDEX idx_ss_baseline_stale
            ON signal_snapshots (baseline_calculated_at NULLS FIRST)
            """
        )


def downgrade() -> None:
    """Drop the locked T3-S10-A signal_snapshots schema."""
    op.drop_index("idx_ss_baseline_stale", table_name="signal_snapshots")
    op.drop_index("idx_ss_org_detector", table_name="signal_snapshots")
    op.drop_index("idx_ss_org_run", table_name="signal_snapshots")
    op.drop_index("idx_ss_org_signal_time", table_name="signal_snapshots")
    op.drop_table("signal_snapshots")
