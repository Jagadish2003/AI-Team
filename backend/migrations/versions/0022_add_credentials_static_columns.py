"""Add static-credential columns to the credentials table (R17-D3 Addendum A, T10).

T10 gave the vault a second record type — static (non-OAuth) credentials (Jira API
token, ServiceNow user/password, native DB connection credentials) — by adding
``kind`` / ``enc_username`` / ``enc_secret`` / ``base_url`` to the ``credentials``
table. Those columns were added to the model DDL (``CREATE_CREDENTIALS_TABLE``) and
to ``provision.sql``, but — unlike 0016 for other tables — no Alembic migration was
written, because ``credentials`` had historically been a "lazy DDL" table created at
runtime (see 0016's note). Runtime creation is now a no-op (the table is provisioned
externally), so an EXISTING deployment migrated via ``alembic upgrade head`` never
received these columns, and every ``get_credential`` / ``get_token`` read (which
selects ``kind``) fails against it. This migration closes that gap so existing DBs
match the model and provision schema.

Imports the DDL from the single source of truth (``database/models/credentials.py``,
``ALTER_CREDENTIALS_ADD_STATIC_CREDENTIAL_COLUMNS``) — the same pattern as 0003 — so
the migration and the model can never drift.

Idempotent AND order-safe. ``credentials`` is not created by any migration (it is
provisioned by provision.sql or the conftest lazy-DDL block, historically at
runtime), so in an alembic-only run the table may not exist yet when this revision
executes. The ALTER is therefore guarded on table existence: it applies the columns
to an EXISTING pre-T10 table, and no-ops when the table is absent (it will later be
created WITH the columns from ``CREATE_CREDENTIALS_TABLE`` / provision.sql). The
``ADD COLUMN IF NOT EXISTS`` also makes it a no-op on a table that already has them.

Revision ID: 0022
Revises: 0021
Create Date: 2026-07-04
"""
import os
import sys
from typing import Sequence, Union

from alembic import op

revision: str = "0022"
down_revision: Union[str, None] = "0021"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_STATIC_COLUMNS = ("kind", "enc_username", "enc_secret", "base_url")


def _add_static_columns_ddl() -> str:
    """Return the idempotent ALTER from the single source of truth.

    ``database/models/credentials.py`` owns the credentials schema; importing the
    ALTER string here keeps this migration and the model DDL identical (as 0003
    does for the entities schema). ``env.py`` declares this project "raw-SQL
    migrations only"; importing a raw SQL string does not change that.
    """
    backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    if backend_dir not in sys.path:
        sys.path.insert(0, backend_dir)
    from database.models.credentials import ALTER_CREDENTIALS_ADD_STATIC_CREDENTIAL_COLUMNS

    return ALTER_CREDENTIALS_ADD_STATIC_CREDENTIAL_COLUMNS


def _guarded(inner_sql: str) -> str:
    """Wrap a credentials ALTER so it runs only when the table already exists.

    Guards the alembic-only ordering case (the table is created outside migrations,
    so it may not exist yet at this revision) — the statement no-ops rather than
    raising UndefinedTable and aborting the upgrade."""
    return (
        "DO $mig$\nBEGIN\n"
        "  IF to_regclass('public.credentials') IS NOT NULL THEN\n"
        f"    {inner_sql};\n"
        "  END IF;\nEND\n$mig$;"
    )


def upgrade() -> None:
    op.execute(_guarded(_add_static_columns_ddl()))


def downgrade() -> None:
    for column in _STATIC_COLUMNS:
        op.execute(_guarded(f"ALTER TABLE credentials DROP COLUMN IF EXISTS {column}"))
