"""Create the ingestion_checkpoints table — R16-A1 / AT-378 (Change-Based Ingestion).

One opaque position marker per ``(org_id, connector_id)``: the checkpoint a
connector reports after a successful run, so the next run processes only what
changed since (R16-A1 §2). The runner treats ``value`` as opaque and writes a row
only after a run fully consumes the delta — a failed/partial run never advances it.

The DDL is imported from database/models/ingestion_checkpoints.py (the single
source of truth shared with the runtime repository), never hardcoded here — same
pattern as 0015_create_org_licenses.py / 0003_create_entities.py.

Idempotent: guarded by inspector.has_table, so re-running (or running against a DB
whose table already exists) is a no-op. Rollback drops the table.

Revision ID: 0017
Revises: 0016
Create Date: 2026-06-23
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from database.models.ingestion_checkpoints import CREATE_INGESTION_CHECKPOINTS_TABLE

revision: str = "0017"
down_revision: Union[str, None] = "0016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("ingestion_checkpoints"):
        return
    op.execute(CREATE_INGESTION_CHECKPOINTS_TABLE)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("ingestion_checkpoints"):
        return
    op.drop_table("ingestion_checkpoints")
