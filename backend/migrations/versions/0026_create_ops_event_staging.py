"""Create the ops_event staging schema — MSP-B8 (Event-History Bridge, T1).

The versioned staging database contract a partner engineer loads exported AWS and
Azure event history into, before the bridge ingestor (T4) reads it on the existing
read-only, fail-closed DB path and maps each raw payload through the MSP-B0
mappers. Two tables:

  * ``ops_event_staging`` — the operational event staging table: monotonic
    ``row_id`` checkpoint key, org scope, provider, source format, export batch
    id, provider event identity (idempotency), the intact raw JSON payload, and a
    load timestamp. UNIQUE ``(org_id, provider, provider_event_id)`` dedupes
    re-loaded batches (AC3); the ``(org_id, row_id)`` index serves incremental
    row-id paging (AC4) and org scoping (AC6).
  * ``ops_event_load_batches`` — the companion batch registry (record/skip counts
    per load).

The DDL lives in ``database/models/ops_event_staging.py`` (``ALL_OPS_EVENT_STAGING_DDL``)
and is imported here so this migration (the CI gate) and the shipped partner
PostgreSQL artifact never drift — the same no-drift pattern as
``0024_create_retrieval_chunks.py`` / ``0003_create_entities.py``. This migration
is how the staging store is created when AgentIQ's own PostgreSQL hosts it; a
partner-provisioned store applies the equivalent ``database/staging/*.sql``
artifact instead.

Idempotent (every statement is ``IF NOT EXISTS``); rollback drops the indexes then
the tables.

Revision ID: 0026
Revises: 0025
Create Date: 2026-07-14
"""
import os
import sys
from typing import Sequence, Union

from alembic import op

revision: str = "0026"
down_revision: Union[str, None] = "0025"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _staging_ddl() -> "tuple[str, ...]":
    """Return the locked staging DDL from its single source of truth.

    The schema lives in ``database/models/ops_event_staging.py`` and is imported
    here so the migration and the shipped partner artifact run identical
    statements. ``env.py`` is raw-SQL-migrations only; importing a tuple of SQL
    strings does not change that.
    """
    backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    if backend_dir not in sys.path:
        sys.path.insert(0, backend_dir)
    from database.models.ops_event_staging import ALL_OPS_EVENT_STAGING_DDL

    return ALL_OPS_EVENT_STAGING_DDL


def _staging_drop_ddl() -> "tuple[str, ...]":
    backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    if backend_dir not in sys.path:
        sys.path.insert(0, backend_dir)
    from database.models.ops_event_staging import DROP_OPS_EVENT_STAGING_DDL

    return DROP_OPS_EVENT_STAGING_DDL


def upgrade() -> None:
    for ddl in _staging_ddl():
        op.execute(ddl)


def downgrade() -> None:
    for ddl in _staging_drop_ddl():
        op.execute(ddl)
