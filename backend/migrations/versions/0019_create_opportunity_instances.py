"""Create opportunity_instances table (R16-B1, Part Two / T4).

Stores one per-run observation of an opportunity (its score, confidence,
evidence and narrative at that point in time) keyed by the stable
``opportunity_identity`` plus the ``run_id``. Many instances sharing one
identity are the time series outcome tracking (2.0) compares.

The DDL lives in ``database/models/opportunity_instances.py``
(``ALL_OPPORTUNITY_INSTANCES_DDL``) and is imported here so this migration (the
CI gate) and the runtime ``ensure_opportunity_instances_table()`` helper execute
the exact same statements — they can never drift. Same approach as
``0003_create_entities.py``.

The table carries an ``is_deleted BOOLEAN NOT NULL DEFAULT FALSE`` soft-delete
column (the pattern established in migration 0016), with the identity index made
composite ``(opportunity_identity, is_deleted)`` so the active-instances read is
index-served. Both come from the imported DDL, so they are applied here without
a separate migration. ``downgrade()`` drops the table, which removes the column
and its indexes.

Revision ID: 0019
Revises: 0018
Create Date: 2026-06-24

Note: originally authored as revision 0017 on the opportunity-identity-spine
branch. It collided with 0017_create_ingestion_checkpoints (the change-based
ingestion branch), which produced a duplicate revision id and a two-headed
migration tree. Re-numbered to 0019 and chained after 0018 to linearise the tree
into a single head. The opportunity_instances and ingestion_checkpoints tables
are independent, so apply order does not matter.

Note (R16-B2 integration): originally authored as revision ``0017``, which
collided with ``0017_create_ingestion_checkpoints`` once both feature branches
merged into the R16-B2 base — two migrations sharing one revision id left alembic
with duplicate/multiple heads and broke ``alembic upgrade head`` (the CI gate).
This migration creates a standalone table with no dependency on the
ingestion-checkpoints lineage, so it was re-chained to the end (``0018`` ->
``0019``) to restore a single linear head. Migration content is unchanged.
"""
import os
import sys
from typing import Sequence, Union

from alembic import op

revision: str = "0019"
down_revision: Union[str, None] = "0018"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _all_opportunity_instances_ddl() -> "tuple[str, ...]":
    """Return the locked opportunity_instances DDL from its single source of truth.

    The schema lives in ``database/models/opportunity_instances.py`` and is
    imported here so the migration and the runtime ensure helper run identical
    CREATE TABLE / CREATE INDEX statements. ``env.py`` is raw-SQL-migrations
    only; importing a tuple of SQL strings does not change that.
    """
    backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    if backend_dir not in sys.path:
        sys.path.insert(0, backend_dir)
    from database.models.opportunity_instances import ALL_OPPORTUNITY_INSTANCES_DDL

    return ALL_OPPORTUNITY_INSTANCES_DDL


def upgrade() -> None:
    for ddl in _all_opportunity_instances_ddl():
        op.execute(ddl)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_opp_instances_org_run")
    op.execute("DROP INDEX IF EXISTS idx_opp_instances_identity")
    op.execute("DROP TABLE IF EXISTS opportunity_instances")
