"""Create entities table for Stage 2 Knowledge Graph.

T3-S12-A — Entity Extraction from Ingestor Runs.
Schema is locked after T3-S12-A merges — column names and types must not
change without updating all downstream graph queries (T3-S13-A through
T3-S15-A depend on this schema).

resolution_confidence and resolution_status are mandatory columns that drive
graph quality in the relationship mapper (T3-S13-A).

Revision ID: 0003
Revises: 0002
Create Date: 2026-06-06
"""
import os
import sys
from typing import Sequence, Union

from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _all_entities_ddl() -> "tuple[str, ...]":
    """Return the locked entities DDL from the single source of truth.

    The schema lives in ``database/models/entities.py`` (``ALL_ENTITIES_DDL``)
    and is imported here so the migration (the CI gate) and the runtime
    ``ensure_entities_table()`` helper can never drift apart — both execute the
    exact same CREATE TABLE / CREATE INDEX statements. ``env.py`` declares this
    project "raw-SQL migrations only"; importing a tuple of raw SQL strings does
    not change that — there is still no ORM metadata involved.
    """
    backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    if backend_dir not in sys.path:
        sys.path.insert(0, backend_dir)
    from database.models.entities import ALL_ENTITIES_DDL

    return ALL_ENTITIES_DDL


def upgrade() -> None:
    for ddl in _all_entities_ddl():
        op.execute(ddl)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_entities_org_run_count")
    op.execute("DROP INDEX IF EXISTS idx_entities_org_run")
    op.execute("DROP INDEX IF EXISTS idx_entities_org_canonical")
    op.execute("DROP TABLE IF EXISTS entities")
