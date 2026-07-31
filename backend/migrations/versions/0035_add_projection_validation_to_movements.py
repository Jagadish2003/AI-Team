"""Add 2.0-A2 T5 projection-validation fields to opportunity_movements.

Revision ID: 0035
Revises: 0034
Create Date: 2026-07-31

The full calibration payload lives in the movement record JSON. These columns
are the query surface A1 and A3 consume: verdict, pack, detector and confidence.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0035"
down_revision: Union[str, None] = "0034"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE opportunity_movements "
        "ADD COLUMN IF NOT EXISTS projection_validation_verdict "
        "VARCHAR(24) NOT NULL DEFAULT 'not_projected'"
    )
    op.execute(
        "ALTER TABLE opportunity_movements "
        "ADD COLUMN IF NOT EXISTS projection_pack_id VARCHAR(64)"
    )
    op.execute(
        "ALTER TABLE opportunity_movements "
        "ADD COLUMN IF NOT EXISTS projection_pack_version VARCHAR(32)"
    )
    op.execute(
        "ALTER TABLE opportunity_movements "
        "ADD COLUMN IF NOT EXISTS projection_confidence VARCHAR(16)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_opp_movements_org_projection_verdict "
        "ON opportunity_movements (org_id, projection_validation_verdict)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_opp_movements_org_projection_pack "
        "ON opportunity_movements (org_id, projection_pack_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_opp_movements_org_detector "
        "ON opportunity_movements (org_id, detector_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_opp_movements_org_projection_confidence "
        "ON opportunity_movements (org_id, projection_confidence)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_opp_movements_org_projection_confidence")
    op.execute("DROP INDEX IF EXISTS idx_opp_movements_org_detector")
    op.execute("DROP INDEX IF EXISTS idx_opp_movements_org_projection_pack")
    op.execute("DROP INDEX IF EXISTS idx_opp_movements_org_projection_verdict")
    op.execute(
        "ALTER TABLE opportunity_movements DROP COLUMN IF EXISTS projection_confidence"
    )
    op.execute(
        "ALTER TABLE opportunity_movements DROP COLUMN IF EXISTS projection_pack_version"
    )
    op.execute("ALTER TABLE opportunity_movements DROP COLUMN IF EXISTS projection_pack_id")
    op.execute(
        "ALTER TABLE opportunity_movements "
        "DROP COLUMN IF EXISTS projection_validation_verdict"
    )
