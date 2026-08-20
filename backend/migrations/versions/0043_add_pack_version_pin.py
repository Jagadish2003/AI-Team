"""Add 2.0-C1 pack version-rollback columns to the pack lifecycle tables.

Revision ID: 0032
Revises: 0031
Create Date: 2026-07-29

Additive and idempotent (``ADD COLUMN IF NOT EXISTS``): a pack with no pin reads
NULL and behaves exactly as it did before rollback existed, so no backfill is
needed and nothing historical is rewritten (2.0-C1 AC3).
"""
import os
import sys
from typing import Sequence, Union

from alembic import op

revision: str = "0043"
down_revision: Union[str, None] = "0042"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _model_ddl(name: str) -> "tuple[str, ...]":
    backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    if backend_dir not in sys.path:
        sys.path.insert(0, backend_dir)
    from database.models import pack_states

    return getattr(pack_states, name)


def upgrade() -> None:
    for statement in _model_ddl("ALL_PACK_VERSION_DDL"):
        op.execute(statement)


def downgrade() -> None:
    for statement in _model_ddl("DROP_PACK_VERSION_DDL"):
        op.execute(statement)
