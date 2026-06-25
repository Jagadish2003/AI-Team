"""Add is_deleted soft-delete column to ingestion_checkpoints — R16-A1 / AT-383 (T7).

The admin checkpoint-reset action clears a source's checkpoint so the next run
does a full re-read (R16-A1 §3, AC7). The least-privilege application DB role has
UPDATE but not DELETE, so the reset is a SOFT delete (``is_deleted = TRUE``)
rather than a row removal — matching the soft-delete convention used elsewhere in
this schema. read_checkpoint filters ``is_deleted = FALSE``; a re-save reactivates.

DDL is imported from database/models/ingestion_checkpoints.py (single source of
truth), never hardcoded here. ``ADD COLUMN IF NOT EXISTS`` is idempotent.

Revision ID: 0018
Revises: 0017
Create Date: 2026-06-23
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from database.models.ingestion_checkpoints import ALTER_INGESTION_CHECKPOINTS_ADD_IS_DELETED

revision: str = "0018"
down_revision: Union[str, None] = "0017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(ALTER_INGESTION_CHECKPOINTS_ADD_IS_DELETED)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("ingestion_checkpoints"):
        return
    columns = {c["name"] for c in inspector.get_columns("ingestion_checkpoints")}
    if "is_deleted" in columns:
        op.drop_column("ingestion_checkpoints", "is_deleted")
