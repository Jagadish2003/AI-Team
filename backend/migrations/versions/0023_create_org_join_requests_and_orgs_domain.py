"""Create org_join_requests table + add orgs.domain — close the provisioning gap.

Two schema objects existed ONLY in database/provision/provision.sql (added there
directly, pg_dump-style) but had no Alembic migration and no model, so a database
built via the maintained path (`alembic upgrade head`) lacked them while a
provision.sql install had them — the two provisioning paths had diverged:

  * org_join_requests  — the org join-request approval flow table (+ its two
    indexes). A teammate added it to provision.sql; nothing created it in alembic.
  * orgs.domain        — an optional per-org domain column + partial UNIQUE index
    (idx_orgs_domain_unique).

This migration back-fills both into the migration chain from the single sources
of truth — database/models/org_join_requests.py (ALL_ORG_JOIN_REQUESTS_DDL) and
database/models/orgs.py (ALL_ORGS_DOMAIN_DDL) — the same import pattern as 0003 /
0020, so migration and model can't drift. Verified with a provision.sql-vs-alembic
schema diff (throwaway DBs): after 0023 the two paths converge at head 0023.

Idempotent: every statement is CREATE TABLE/INDEX IF NOT EXISTS or ADD COLUMN IF
NOT EXISTS, so it is a no-op on a fresh provision (which already has these) and
applies cleanly to a DB that was built via alembic and is missing them.

NOTE: idx_orgs_domain_unique / the pending-unique index are created on live data;
on a DB that already holds duplicate non-null domains (or duplicate pending
join-requests) index creation fails — deduplicate first. Fresh installs and CI
start empty, so this only affects manually-populated databases.

Revision ID: 0023
Revises: 0022
Create Date: 2026-07-04
"""
import os
import sys
from typing import Sequence, Union

from alembic import op

revision: str = "0023"
down_revision: Union[str, None] = "0022"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _ddl() -> "tuple[tuple[str, ...], tuple[str, ...]]":
    """Return (org_join_requests DDL, orgs.domain DDL) from the model SSOT."""
    backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    if backend_dir not in sys.path:
        sys.path.insert(0, backend_dir)
    from database.models.org_join_requests import ALL_ORG_JOIN_REQUESTS_DDL
    from database.models.orgs import ALL_ORGS_DOMAIN_DDL

    return ALL_ORG_JOIN_REQUESTS_DDL, ALL_ORGS_DOMAIN_DDL


def upgrade() -> None:
    join_requests_ddl, orgs_domain_ddl = _ddl()
    for ddl in join_requests_ddl:
        op.execute(ddl)
    for ddl in orgs_domain_ddl:
        op.execute(ddl)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_orgs_domain_unique")
    op.execute("ALTER TABLE orgs DROP COLUMN IF EXISTS domain")
    op.execute("DROP INDEX IF EXISTS idx_join_requests_pending_unique")
    op.execute("DROP INDEX IF EXISTS idx_join_requests_org_status")
    op.execute("DROP TABLE IF EXISTS org_join_requests")
