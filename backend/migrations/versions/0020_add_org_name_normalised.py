"""Add name_normalised + unique index to orgs — org name deduplication.

Adds a normalised (trimmed + lowercased) copy of the org name and a UNIQUE index
on it, so two registrations of the same company name resolve to ONE org_id
instead of fragmenting into duplicate workspaces. register_org_and_owner writes
name_normalised via user_auth.normalise_org_name(); the UNIQUE index is the
storage-level backstop for the dedup lookup.

DDL lives in database/models/orgs.py (ALL_ORG_NAME_NORMALISED_DDL) and is
imported here so the migration (CI gate) and the pure-SQL provisioning path stay
in sync — same SSOT pattern as 0005/0010. The ordered tuple runs:
  1. ADD COLUMN name_normalised VARCHAR(256) NOT NULL DEFAULT ''  (IF NOT EXISTS)
  2. UPDATE orgs SET name_normalised = LOWER(name) WHERE name_normalised = ''
     (backfill existing rows BEFORE the constraint is enforced)
  3. CREATE UNIQUE INDEX idx_orgs_name_normalised_unique                (IF NOT EXISTS)

The project runs on PostgreSQL only (see tests/contract/conftest.py and
database/provision), so ADD COLUMN IF NOT EXISTS / CREATE UNIQUE INDEX IF NOT
EXISTS / DROP COLUMN are used directly — no SQLite batch_alter_table dance.

NOTE: if a pre-existing populated database already holds two orgs whose names
collide once lowercased, step 3 will fail (duplicate key). A fresh migration
build (CI/test) and a fresh provision start from an empty orgs table, so this
only affects manually-populated dev databases — deduplicate those rows first.

Revision ID: 0020
Revises: 0019
Create Date: 2026-07-02
"""
import os
import sys
from typing import Sequence, Union

from alembic import op

revision: str = "0020"
down_revision: Union[str, None] = "0019"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _name_normalised_ddl() -> "tuple[str, ...]":
    backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    if backend_dir not in sys.path:
        sys.path.insert(0, backend_dir)
    from database.models.orgs import ALL_ORG_NAME_NORMALISED_DDL

    return ALL_ORG_NAME_NORMALISED_DDL


def upgrade() -> None:
    for ddl in _name_normalised_ddl():
        op.execute(ddl)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_orgs_name_normalised_unique")
    op.execute("ALTER TABLE orgs DROP COLUMN IF EXISTS name_normalised")
