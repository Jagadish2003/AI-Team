"""Create ranking_adjustments + history — 2.0-A3 T2 bounded adjustment state.

Revision ID: 0037
Revises: 0036
Create Date: 2026-08-03

The per-org learned adjustment, stored as a VALUE rather than derived at read
time so a ranking cannot shift silently as history accrues, and so T4's audit and
reset have something to read and something to reset.

DDL is imported from ``database/models/ranking_adjustments.py`` rather than
duplicated here, so the migration-applied schema and any runtime-created schema
cannot drift — the arrangement ``0003_create_entities.py`` established.
"""
from typing import Sequence, Union

from alembic import op

from database.models.ranking_adjustments import ALL_RANKING_ADJUSTMENT_DDL

revision: str = "0037"
down_revision: Union[str, None] = "0036"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    for statement in ALL_RANKING_ADJUSTMENT_DDL:
        op.execute(statement)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_ranking_adjustment_history_group")
    op.execute("DROP INDEX IF EXISTS idx_ranking_adjustment_history_org")
    op.execute("DROP TABLE IF EXISTS ranking_adjustment_history")
    op.execute("DROP INDEX IF EXISTS idx_ranking_adjustments_org")
    op.execute("DROP TABLE IF EXISTS ranking_adjustments")
