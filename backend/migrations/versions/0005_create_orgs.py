"""Create orgs table — AUTH-1 / AT-233.

Persists the organization created during registration (id, name, created_at).
org_id remains the opaque tenant key in workspace_members and the tenancy
layer; this table only names it. Membership/role stay in workspace_members.

DDL lives in database/models/orgs.py (ALL_ORGS_DDL) and is imported here so the
migration (CI gate) and the runtime ensure_auth_tables() lazy-init execute the
exact same statement — same SSOT pattern as 0003/0004.

Revision ID: 0005
Revises: 0004
Create Date: 2026-06-09
"""
import os
import sys
from typing import Sequence, Union

from alembic import op

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _orgs_ddl() -> "tuple[str, ...]":
    backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    if backend_dir not in sys.path:
        sys.path.insert(0, backend_dir)
    from database.models.orgs import ALL_ORGS_DDL

    return ALL_ORGS_DDL


def upgrade() -> None:
    for ddl in _orgs_ddl():
        op.execute(ddl)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS orgs")
