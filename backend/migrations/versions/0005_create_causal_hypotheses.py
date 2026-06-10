"""Create causal_hypotheses table for Stage 3 Causal Inference.

T3-S16-A — Causal Chain Hypotheses with Enterprise Quality Gates.
Schema is locked after T3-S16-A merges — column names and types must not
change without updating T3-S17-A (intervention modelling) and T3-S18-A
(outcome tracking) simultaneously.

falsifiability_condition NOT NULL is load-bearing: it is the DB-level
enforcement that a hypothesis without a falsifiability condition is never
stored.

preliminary and preliminary_reason are read by T7 and T9 to drive the
amber 'analyst review required' banner.

Revision ID: 0005
Revises: 0004
Create Date: 2026-06-10
"""
import os
import sys
from typing import Sequence, Union

from alembic import op

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _all_causal_hypotheses_ddl() -> "tuple[str, ...]":
    """Return the locked causal_hypotheses DDL from the single source of truth."""
    backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    if backend_dir not in sys.path:
        sys.path.insert(0, backend_dir)
    from database.models.causal_hypotheses import ALL_CAUSAL_HYPOTHESES_DDL

    return ALL_CAUSAL_HYPOTHESES_DDL


def upgrade() -> None:
    for ddl in _all_causal_hypotheses_ddl():
        op.execute(ddl)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_ch_org_preliminary")
    op.execute("DROP INDEX IF EXISTS idx_ch_org_opp")
    op.execute("DROP TABLE IF EXISTS causal_hypotheses")
