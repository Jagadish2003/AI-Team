"""Create opportunity_feedback — 2.0-A3 T1 learning decision record.

Revision ID: 0036
Revises: 0035
Create Date: 2026-08-03

The durable analyst accept/dismiss/defer record the learning signal set reads,
keyed on the stable ``opportunity_identity`` rather than a run-scoped id.

DDL is imported from ``database/models/opportunity_feedback.py`` rather than
duplicated here, so the migration-applied schema and any runtime-created schema
cannot drift — the same arrangement ``0003_create_entities.py`` established.
"""
from typing import Sequence, Union

from alembic import op

from database.models.opportunity_feedback import ALL_OPPORTUNITY_FEEDBACK_DDL

revision: str = "0036"
down_revision: Union[str, None] = "0035"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    for statement in ALL_OPPORTUNITY_FEEDBACK_DDL:
        op.execute(statement)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_opportunity_feedback_org_recorded")
    op.execute("DROP INDEX IF EXISTS idx_opportunity_feedback_similarity")
    op.execute("DROP INDEX IF EXISTS idx_opportunity_feedback_identity")
    op.execute("DROP TABLE IF EXISTS opportunity_feedback")
