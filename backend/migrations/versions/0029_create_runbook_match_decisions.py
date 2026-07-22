"""Create the MSP-B5 runbook-match decision and feedback stores.

Revision ID: 0029
Revises: 0028
Create Date: 2026-07-21
"""
import os
import sys
from typing import Sequence, Union

from alembic import op

revision: str = "0029"
down_revision: Union[str, None] = "0028"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _ddl() -> "tuple[str, ...]":
    backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    if backend_dir not in sys.path:
        sys.path.insert(0, backend_dir)
    from database.models.runbook_match_decisions import ALL_RUNBOOK_MATCH_DDL

    return ALL_RUNBOOK_MATCH_DDL


def _drop_ddl() -> "tuple[str, ...]":
    backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    if backend_dir not in sys.path:
        sys.path.insert(0, backend_dir)
    from database.models.runbook_match_decisions import DROP_RUNBOOK_MATCH_DDL

    return DROP_RUNBOOK_MATCH_DDL


def upgrade() -> None:
    for statement in _ddl():
        op.execute(statement)


def downgrade() -> None:
    for statement in _drop_ddl():
        op.execute(statement)
