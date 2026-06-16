"""Create audit_log table — AT-82 T3 (multi-tenant audit trail).

Until now audit_log was created only by the lazy runtime helper
(app/middleware/audit.py::_ensure_table) on the first log_event() call, and
never by a migration. A database brought up with `alembic upgrade head` alone
(no app startup, or a startup where the lazy creation failed silently) was left
without the table. This migration brings audit_log in line with every other
native table — created deterministically by Alembic — while the runtime
CREATE TABLE IF NOT EXISTS path stays as a harmless backstop.

DDL is imported from the single source of truth (database/models/audit_log.py)
so the migration-applied schema and the runtime-created schema never drift.

Revision ID: 0009
Revises: 0008
Create Date: 2026-06-16
"""
import os
import sys
from typing import Sequence, Union

from alembic import op

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _audit_log_ddl() -> "tuple[str, ...]":
    """Return the audit_log DDL from the single source of truth."""
    backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    if backend_dir not in sys.path:
        sys.path.insert(0, backend_dir)
    from database.models.audit_log import (
        CREATE_AUDIT_LOG_IDX_ORG_EVENT,
        CREATE_AUDIT_LOG_IDX_ORG_TS,
        CREATE_AUDIT_LOG_TABLE,
    )

    return (
        CREATE_AUDIT_LOG_TABLE,
        CREATE_AUDIT_LOG_IDX_ORG_TS,
        CREATE_AUDIT_LOG_IDX_ORG_EVENT,
    )


def upgrade() -> None:
    for ddl in _audit_log_ddl():
        op.execute(ddl)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_audit_org_event")
    op.execute("DROP INDEX IF EXISTS idx_audit_org_ts")
    op.execute("DROP TABLE IF EXISTS audit_log")
