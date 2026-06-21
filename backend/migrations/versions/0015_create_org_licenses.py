"""Create the org_licenses table — LIC-1 per-org licensing.

Moves the installed license key from a single installation-global KV slot
(``license:key``) to a per-organisation table so each tenant has its own license:
a freshly registered org starts with no row → status ``no_license`` (the License
page renders red / "No valid license installed") until its Owner pastes a valid
key, and one org's key never satisfies another org's license check.

The DDL is imported from database/models/org_licenses.py (the single source of
truth shared with the runtime), never hardcoded here — same pattern as
0003_create_entities.py importing ALL_ENTITIES_DDL.

Idempotent: guarded by inspector.has_table, so re-running (or running against a
DB whose table already exists) is a no-op. Rollback drops the table.

Revision ID: 0015
Revises: 0014
Create Date: 2026-06-20
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from database.models.org_licenses import CREATE_ORG_LICENSES_TABLE

revision: str = "0015"
down_revision: Union[str, None] = "0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("org_licenses"):
        return
    op.execute(CREATE_ORG_LICENSES_TABLE)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("org_licenses"):
        return
    op.drop_table("org_licenses")
