"""Add is_deleted soft-delete column to login_attempts and workspace_members.

Part of the soft-delete change: the application DB role is granted SELECT/INSERT/
UPDATE but NOT DELETE, so the physical DELETEs the app used to issue are converted
to ``UPDATE … SET is_deleted = TRUE`` and every read filters ``is_deleted = FALSE``.

This migration covers the two **alembic-managed** tables that the app soft-deletes
from: ``login_attempts`` (failed-attempt cleanup on successful login) and
``workspace_members`` (member removal). The other soft-deleted tables — run_events
(seed_loader), credentials / nonces / oauth_nonces (lazy DDL) — are not alembic
managed; their column is added in their own DDL sources + provision.sql.

Idempotent: ADD COLUMN IF NOT EXISTS, so it is a no-op on a DB whose model DDL
already created the column (fresh provision) and applies it to existing DBs.

Revision ID: 0016
Revises: 0015
Create Date: 2026-06-21
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0016"
down_revision: Union[str, None] = "0015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLES = ("login_attempts", "workspace_members")


def upgrade() -> None:
    for table in _TABLES:
        op.execute(
            f"ALTER TABLE {table} "
            "ADD COLUMN IF NOT EXISTS is_deleted BOOLEAN NOT NULL DEFAULT FALSE"
        )


def downgrade() -> None:
    for table in _TABLES:
        op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS is_deleted")
