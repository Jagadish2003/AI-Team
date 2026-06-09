"""Create entity_relationships table for Stage 2 Knowledge Graph.

T3-S13-A — Relationship Mapping.
Schema is locked after T3-S13-A merges — column names and types must not
change without updating T3-S14-A (graph queries) and T3-S15-A (LLM context
builder) simultaneously.

inferred and confidence are load-bearing columns that drive graph query
filtering and LLM prompt construction in downstream sprints.

Revision ID: 0004
Revises: 0003
Create Date: 2026-06-09
"""
import os
import sys
from typing import Sequence, Union

from alembic import op

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _all_entity_relationships_ddl() -> "tuple[str, ...]":
    """Return the locked entity_relationships DDL from the single source of truth."""
    backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    if backend_dir not in sys.path:
        sys.path.insert(0, backend_dir)
    from database.models.entity_relationships import ALL_ENTITY_RELATIONSHIPS_DDL

    return ALL_ENTITY_RELATIONSHIPS_DDL


def upgrade() -> None:
    for ddl in _all_entity_relationships_ddl():
        op.execute(ddl)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_er_org_type")
    op.execute("DROP INDEX IF EXISTS idx_er_org_to")
    op.execute("DROP INDEX IF EXISTS idx_er_org_from")
    op.execute("DROP TABLE IF EXISTS entity_relationships")
