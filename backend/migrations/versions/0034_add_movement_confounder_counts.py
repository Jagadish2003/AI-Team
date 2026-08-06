"""Add 2.0-A2 T4 confounder caveat counts to opportunity_movements.

Revision ID: 0034
Revises: 0033
Create Date: 2026-07-30

The caveats themselves live in the record JSON; these columns are the promoted
counts T6's portfolio aggregate needs so it can report how many of its inputs
carried a caveat without parsing every record.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0034"
down_revision: Union[str, None] = "0033"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE opportunity_movements "
        "ADD COLUMN IF NOT EXISTS confounder_count INTEGER NOT NULL DEFAULT 0"
    )
    op.execute(
        "ALTER TABLE opportunity_movements "
        "ADD COLUMN IF NOT EXISTS confounder_material_count INTEGER NOT NULL DEFAULT 0"
    )
    op.execute(
        "ALTER TABLE opportunity_movements ADD COLUMN IF NOT EXISTS confounder_types TEXT"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_opp_movements_org_confounders "
        "ON opportunity_movements (org_id, confounder_count)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_opp_movements_org_confounders")
    op.execute("ALTER TABLE opportunity_movements DROP COLUMN IF EXISTS confounder_types")
    op.execute(
        "ALTER TABLE opportunity_movements DROP COLUMN IF EXISTS confounder_material_count"
    )
    op.execute("ALTER TABLE opportunity_movements DROP COLUMN IF EXISTS confounder_count")
