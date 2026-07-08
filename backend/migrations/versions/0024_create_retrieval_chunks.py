"""Create the pgvector-backed retrieval_chunks store (R18-B1 — Retrieval Substrate, T1).

The org-partitioned vector store every deep-content story in Release 1.8 builds
on. Uses pgvector in the EXISTING PostgreSQL — no new infrastructure component
for on-prem installs (Section 6: "pgvector, not a new component"). The store is
hard-partitioned by ``org_id`` at the SQL layer so one customer's indexed content
is never retrievable by another (AC3), consistent with R17-D3 tenant isolation.

The DDL lives in ``database/models/retrieval.py`` (``ALL_RETRIEVAL_DDL``) and is
imported here so this migration (the CI gate) and the runtime
``app.retrieval.store.ensure_retrieval_store()`` helper execute the exact same
statements — they can never drift. Same approach as ``0003_create_entities.py``.

Requires the pgvector extension. ``upgrade()`` runs ``CREATE EXTENSION IF NOT
EXISTS vector`` first; the migration role must be permitted to create it (on
managed PostgreSQL this is typically a one-time grant). ``downgrade()`` drops the
table and its indexes but intentionally leaves the extension in place — dropping a
shared extension on a store rollback is unsafe.

Revision ID: 0024
Revises: 0023
Create Date: 2026-07-07
"""
import os
import sys
from typing import Sequence, Union

from alembic import op

revision: str = "0024"
down_revision: Union[str, None] = "0023"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _retrieval_ddl() -> "tuple[str, ...]":
    """Return the locked retrieval_chunks DDL from its single source of truth.

    The schema lives in ``database/models/retrieval.py`` and is imported here so
    the migration and the runtime ensure helper run identical statements.
    ``env.py`` is raw-SQL-migrations only; importing a tuple of SQL strings does
    not change that.
    """
    backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    if backend_dir not in sys.path:
        sys.path.insert(0, backend_dir)
    from database.models.retrieval import ALL_RETRIEVAL_DDL

    return ALL_RETRIEVAL_DDL


def _retrieval_drop_ddl() -> "tuple[str, ...]":
    backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    if backend_dir not in sys.path:
        sys.path.insert(0, backend_dir)
    from database.models.retrieval import DROP_RETRIEVAL_DDL

    return DROP_RETRIEVAL_DDL


def upgrade() -> None:
    for ddl in _retrieval_ddl():
        op.execute(ddl)


def downgrade() -> None:
    for ddl in _retrieval_drop_ddl():
        op.execute(ddl)
