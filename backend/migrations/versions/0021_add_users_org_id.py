"""Add users.org_id FK -> orgs.id — denormalized org pointer.

Adds a nullable org_id column to users and a foreign key to orgs(id). At
registration user_auth writes the org resolved by the case-insensitive dedup
logic (reused-or-new) into this column, so every registered user carries its
organization's UUID directly. workspace_members remains the source of truth for
org_id + role (login is unchanged); this column is a convenience pointer only.

DDL lives in database/models/users.py (ALL_USERS_ORG_ID_DDL) and is imported here
so the migration (CI gate) and the pure-SQL provisioning path stay in sync — same
SSOT pattern as 0005/0010/0020. The ordered tuple runs:
  1. ADD COLUMN org_id VARCHAR(36)                              (IF NOT EXISTS)
  2. Backfill existing users from their earliest workspace membership
     (BEFORE the FK, so no pre-existing row can violate it)
  3. ADD CONSTRAINT fk_users_org_id FOREIGN KEY (org_id) REFERENCES orgs (id)
     (guarded — ADD CONSTRAINT has no IF NOT EXISTS)

The column is nullable, so legacy users with no resolvable membership, and the
untouched invite flow, remain valid. The FK references orgs, created by migration
0005 (after 0004 builds users), which is why org_id is added here rather than in
the base users CREATE. PostgreSQL only (see conftest / database/provision).

Revision ID: 0021
Revises: 0020
Create Date: 2026-07-02
"""
import os
import sys
from typing import Sequence, Union

from alembic import op

revision: str = "0021"
down_revision: Union[str, None] = "0020"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _users_org_id_ddl() -> "tuple[str, ...]":
    backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    if backend_dir not in sys.path:
        sys.path.insert(0, backend_dir)
    from database.models.users import ALL_USERS_ORG_ID_DDL

    return ALL_USERS_ORG_ID_DDL


def upgrade() -> None:
    for ddl in _users_org_id_ddl():
        op.execute(ddl)


def downgrade() -> None:
    op.execute("ALTER TABLE users DROP CONSTRAINT IF EXISTS fk_users_org_id")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS org_id")
