"""Add retrieval-freshness schema (R18-B2 — Retrieval Freshness, T1).

Extends the R18-B1 ``retrieval_chunks`` store with the freshness machinery the
subscriber (T1) and the async refresh worker (T3) need:

  * ``retrieval_chunks.is_stale`` + ``stale_at`` — the flag set when a source
    artifact changes and cleared when it is refreshed; T4 excludes stale chunks
    from default retrieval, T6 counts them.
  * ``retrieval_refresh_queue`` — the durable work list of artifacts awaiting
    re-chunk + re-embed; the subscriber enqueues, the worker drains.

The DDL lives in ``database/models/retrieval_freshness.py`` (``ALL_FRESHNESS_DDL``)
and is imported here so this migration (the CI gate) and the runtime
``app.retrieval.store.ensure_freshness_schema()`` helper execute the exact same
statements — they can never drift. Same approach as 0024/0003.

``downgrade()`` drops the queue table/indexes and the freshness columns. It does
NOT drop ``retrieval_chunks`` (owned by 0024) or the pgvector extension.

Revision ID: 0025
Revises: 0024
Create Date: 2026-07-08
"""
import os
import sys
from typing import Sequence, Union

from alembic import op

revision: str = "0025"
down_revision: Union[str, None] = "0024"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _freshness_ddl() -> "tuple[str, ...]":
    """Return the locked freshness DDL from its single source of truth.

    The schema lives in ``database/models/retrieval_freshness.py`` and is imported
    here so the migration and the runtime ensure helper run identical statements.
    ``env.py`` is raw-SQL-migrations only; importing a tuple of SQL strings does
    not change that.
    """
    backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    if backend_dir not in sys.path:
        sys.path.insert(0, backend_dir)
    from database.models.retrieval_freshness import ALL_FRESHNESS_DDL

    return ALL_FRESHNESS_DDL


def _freshness_drop_ddl() -> "tuple[str, ...]":
    backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    if backend_dir not in sys.path:
        sys.path.insert(0, backend_dir)
    from database.models.retrieval_freshness import DROP_FRESHNESS_DDL

    return DROP_FRESHNESS_DDL


def upgrade() -> None:
    for ddl in _freshness_ddl():
        op.execute(ddl)


def downgrade() -> None:
    for ddl in _freshness_drop_ddl():
        op.execute(ddl)
