"""Create workspace_members table — AT-82 T6 (RBAC membership).

Until now workspace_members was created only by lazy runtime helpers
(app/rbac.py::_ensure_members_table, app/auth/user_auth.py::ensure_auth_tables)
and seeded by seed_owner() at app startup — never by a migration. A database
brought up with `alembic upgrade head` alone was left without the table, so
RBAC role lookups (require_role) had nothing to read. This migration creates it
deterministically; the runtime CREATE TABLE IF NOT EXISTS paths remain as a
harmless backstop and continue to own row seeding (seed_owner).

DDL is imported from the single source of truth
(database/models/workspace_members.py) so the migration-applied schema and the
runtime-created schema never drift.

Revision ID: 0010
Revises: 0009
Create Date: 2026-06-16
"""
import os
import sys
from typing import Sequence, Union

from alembic import op

revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _workspace_members_ddl() -> str:
    """Return the workspace_members DDL from the single source of truth."""
    backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    if backend_dir not in sys.path:
        sys.path.insert(0, backend_dir)
    from database.models.workspace_members import CREATE_WORKSPACE_MEMBERS_TABLE

    return CREATE_WORKSPACE_MEMBERS_TABLE


def upgrade() -> None:
    op.execute(_workspace_members_ddl())


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS workspace_members")
