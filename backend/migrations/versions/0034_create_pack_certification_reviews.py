"""Create the 2.0-C2 pack certification review trail.

Revision ID: 0034
Revises: 0033
Create Date: 2026-07-31

2.0-C2 T2 (AT-832 / AC5): every certification decision is recorded with reviewer,
criteria, and date, and is auditable.

Two steps, in this order:

1. Create ``pack_certification_reviews`` (append-only review trail).
2. Re-apply the 0033 REVOKE block. ``pack_certification_reviews`` joins
   ``app.history_retention.PROTECTED_TABLES``, and 0033 has already run on existing
   deployments — so without this second step the new table would be created under
   ``GRANT ALL PRIVILEGES`` (which includes DELETE) and have no data-layer
   enforcement at all. The block is generated from the protected set, guarded per
   role and per table, and idempotent, so re-running it is a no-op for the tables
   0033 already covered.
"""
import os
import sys
from typing import Sequence, Union

from alembic import op

revision: str = "0034"
down_revision: Union[str, None] = "0033"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _backend_on_path() -> None:
    backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    if backend_dir not in sys.path:
        sys.path.insert(0, backend_dir)


def _review_ddl(name: str) -> "tuple[str, ...]":
    _backend_on_path()
    from database.models import pack_certification_reviews

    return getattr(pack_certification_reviews, name)


def _retention_ddl(name: str) -> "tuple[str, ...]":
    _backend_on_path()
    from database.models import history_retention

    return getattr(history_retention, name)


def upgrade() -> None:
    for statement in _review_ddl("ALL_PACK_CERTIFICATION_REVIEW_DDL"):
        op.execute(statement)
    # The new table is protected history — revoke DELETE/TRUNCATE on it too.
    for statement in _retention_ddl("ALL_HISTORY_RETENTION_DDL"):
        op.execute(statement)


def downgrade() -> None:
    for statement in _review_ddl("DROP_PACK_CERTIFICATION_REVIEW_DDL"):
        op.execute(statement)
