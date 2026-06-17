"""Add current_step column to runs table for per-step discovery tracking.

CS-4 T3 — update_run_step() / DISCOVERY_STEPS integration.

Adds current_step VARCHAR to the runs table so the status-poll endpoint can
read which discovery stage is actively in progress without a JSON-payload parse.
The column is nullable: existing rows (and new rows before the first step is
recorded) carry NULL, which the status endpoint surfaces as None / absent.

Revision ID: 0008
Revises: 0007
Create Date: 2026-06-16
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE runs ADD COLUMN current_step VARCHAR")


def downgrade() -> None:
    # SQLite does not support DROP COLUMN in older versions; recreate without it.
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS runs_backup AS
        SELECT id, payload FROM runs
        """
    )
    op.execute("DROP TABLE runs")
    op.execute(
        "CREATE TABLE runs (id TEXT PRIMARY KEY, payload TEXT NOT NULL)"
    )
    op.execute("INSERT INTO runs SELECT id, payload FROM runs_backup")
    op.execute("DROP TABLE runs_backup")
