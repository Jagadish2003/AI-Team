"""Add current_step column to runs table for per-step discovery tracking.

CS-4 T3 — update_run_step() / DISCOVERY_STEPS integration.

Adds current_step VARCHAR to the runs table so the status-poll endpoint can
read which discovery stage is actively in progress without a JSON-payload parse.
The column is nullable: existing rows (and new rows before the first step is
recorded) carry NULL, which the status endpoint surfaces as None / absent.

Revision ID: 0011
Revises: 0010
Create Date: 2026-06-16
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0011"
down_revision: Union[str, None] = "0010"
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
