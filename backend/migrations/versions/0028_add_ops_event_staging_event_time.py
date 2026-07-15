"""Add ops_event_staging.event_time — MSP-B8 (Event-History Bridge, T3).

The Azure loaders (and, retrofitted, the AWS loaders) write the provider event
timestamp "where available" into staging, so the bridge can order/dedupe on it
without parsing the raw payload. Nullable and additive (schema contract v1.1.0):
existing rows and any loader that cannot find a timestamp simply leave it NULL.

The column is defined in the single source of truth
(``database/models/ops_event_staging.py`` — ``ALTER_OPS_EVENT_STAGING_ADD_EVENT_TIME``,
also included in ``ALL_OPS_EVENT_STAGING_DDL``) and imported here, so the migration
path (0027 create → 0028 alter), the runtime ensure helper, and a fresh create all
converge on the same column — the no-drift discipline. Idempotent
(``ADD COLUMN IF NOT EXISTS``); rollback drops the column.

Revision ID: 0028
Revises: 0027
Create Date: 2026-07-14
"""
import os
import sys
from typing import Sequence, Union

from alembic import op

revision: str = "0028"
down_revision: Union[str, None] = "0027"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _add_event_time_ddl() -> str:
    backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    if backend_dir not in sys.path:
        sys.path.insert(0, backend_dir)
    from database.models.ops_event_staging import ALTER_OPS_EVENT_STAGING_ADD_EVENT_TIME

    return ALTER_OPS_EVENT_STAGING_ADD_EVENT_TIME


def upgrade() -> None:
    op.execute(_add_event_time_ddl())


def downgrade() -> None:
    op.execute("ALTER TABLE ops_event_staging DROP COLUMN IF EXISTS event_time")
