"""Revoke DELETE/TRUNCATE on run-history tables from the app login roles.

Revision ID: 0033
Revises: 0032
Create Date: 2026-07-30

2.0-C1 T4 (AT-829 / AC4): findings, evidence, run records, and the pack lifecycle
audit trail can never be deleted by the application — the database refuses it.

Idempotent (REVOKE on an already-revoked privilege is a no-op) and guarded per role
and per table, so it is safe on a deployment whose app roles or table set differ.
Adds no tables and rewrites no rows.
"""
import os
import sys
from typing import Sequence, Union

from alembic import op

revision: str = "0044"
down_revision: Union[str, None] = "0043"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _model_ddl(name: str) -> "tuple[str, ...]":
    backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    if backend_dir not in sys.path:
        sys.path.insert(0, backend_dir)
    from database.models import history_retention

    return getattr(history_retention, name)


def upgrade() -> None:
    for statement in _model_ddl("ALL_HISTORY_RETENTION_DDL"):
        op.execute(statement)


def downgrade() -> None:
    # Re-grants DELETE/TRUNCATE — i.e. re-opens the ability to delete run history.
    # Present only so the migration is reversible.
    for statement in _model_ddl("DROP_HISTORY_RETENTION_DDL"):
        op.execute(statement)
