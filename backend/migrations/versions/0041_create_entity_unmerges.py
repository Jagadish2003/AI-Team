"""Create the 2.0-B2 T5 unmerge suppression + finding re-evaluation flag tables.

Revision ID: 0041
Revises: 0040
Create Date: 2026-08-03
"""
import os
import sys
from typing import Sequence, Union

from alembic import op

revision: str = "0041"
down_revision: Union[str, None] = "0040"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _backend_on_path() -> None:
    backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    if backend_dir not in sys.path:
        sys.path.insert(0, backend_dir)


def _ddl() -> "tuple[str, ...]":
    # The DDL lives in database/models/entity_unmerges.py so the migration-applied
    # schema and any runtime reader execute the exact same statements and can never
    # drift (the locked entities pattern).
    _backend_on_path()
    from database.models.entity_unmerges import ALL_ENTITY_UNMERGE_DDL

    return ALL_ENTITY_UNMERGE_DDL


def _drop_ddl() -> "tuple[str, ...]":
    _backend_on_path()
    from database.models.entity_unmerges import DROP_ENTITY_UNMERGE_DDL

    return DROP_ENTITY_UNMERGE_DDL


def upgrade() -> None:
    for statement in _ddl():
        op.execute(statement)


def downgrade() -> None:
    for statement in _drop_ddl():
        op.execute(statement)
