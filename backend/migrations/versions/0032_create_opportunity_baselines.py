"""Create the 2.0-A2 immutable opportunity-baseline artifact table.

Revision ID: 0032
Revises: 0031
Create Date: 2026-07-30
"""
import os
import sys
from typing import Sequence, Union

from alembic import op

revision: str = "0032"
down_revision: Union[str, None] = "0031"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _backend_on_path() -> None:
    backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    if backend_dir not in sys.path:
        sys.path.insert(0, backend_dir)


def _ddl() -> "tuple[str, ...]":
    # DDL lives in database/models/opportunity_baselines.py so the
    # migration-applied schema and the runtime ensure_* helper execute the exact
    # same statements and can never drift (the locked entities pattern).
    _backend_on_path()
    from database.models.opportunity_baselines import ALL_OPPORTUNITY_BASELINES_DDL

    return ALL_OPPORTUNITY_BASELINES_DDL


def _drop_ddl() -> "tuple[str, ...]":
    _backend_on_path()
    from database.models.opportunity_baselines import DROP_OPPORTUNITY_BASELINES_DDL

    return DROP_OPPORTUNITY_BASELINES_DDL


def upgrade() -> None:
    for statement in _ddl():
        op.execute(statement)


def downgrade() -> None:
    for statement in _drop_ddl():
        op.execute(statement)
