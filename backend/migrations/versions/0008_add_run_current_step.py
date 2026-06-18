"""Add current_step column to runs table for per-step discovery tracking.

CS-4 T3 — update_run_step() / DISCOVERY_STEPS integration.

Adds a nullable current_step VARCHAR to the runs table as a denormalized SQL
mirror of the step that db.update_run_step() also writes into the run JSON
payload. This column exists for SQL-level queryability/observability (e.g.
"which runs are stuck at sf_ncino") — it is NOT the API read path: the run
status endpoint (routes_sprint4_t2.run_status) reads current_step from the run
JSON payload via get_run(), not from this column. Existing rows (and new rows
before the first step is recorded) carry NULL, surfaced as None / absent.

Revision ID: 0008
Revises: 0007
Create Date: 2026-06-16
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not inspector.has_table("runs"):
        op.create_table(
            "runs",
            sa.Column("id", sa.Text(), primary_key=True),
            sa.Column("payload", sa.Text(), nullable=False),
            sa.Column("current_step", sa.String(), nullable=True),
        )
        return

    columns = {column["name"] for column in inspector.get_columns("runs")}
    if "current_step" not in columns:
        op.add_column("runs", sa.Column("current_step", sa.String(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("runs"):
        return

    columns = {column["name"] for column in inspector.get_columns("runs")}
    if "current_step" not in columns:
        return

    with op.batch_alter_table("runs") as batch_op:
        batch_op.drop_column("current_step")
