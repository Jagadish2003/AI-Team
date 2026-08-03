"""Add the 2.0-B2 T4 stable identity key to entity_match_proposals.

A decision keyed only on entity ROW ids misses its own pair when those ids churn —
a source that starts supplying record ids makes ``upsert_source_entity`` insert a
NEW resolved row for an entity previously known by name only — and the pair is then
re-proposed despite having been answered (AC3). ``identity_key`` is the pair's
stable source identity, so the decision survives that churn.

Nullable and additive: rows written before T4 keep NULL and are backfilled from
their stored ``evidence_payload`` by ``app.entity_match_proposals`` on the next
scan, so no data migration runs here.

Revision ID: 0033
Revises: 0032
Create Date: 2026-08-03
"""
import os
import sys
from typing import Sequence, Union

from alembic import op

revision: str = "0033"
down_revision: Union[str, None] = "0032"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _backend_on_path() -> None:
    backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    if backend_dir not in sys.path:
        sys.path.insert(0, backend_dir)


def _ddl() -> "tuple[str, ...]":
    # The DDL lives in database/models/entity_match_proposals.py so the
    # migration-applied schema and a freshly provisioned one cannot drift.
    _backend_on_path()
    from database.models.entity_match_proposals import (
        ALL_ENTITY_MATCH_PROPOSAL_T4_DDL,
    )

    return ALL_ENTITY_MATCH_PROPOSAL_T4_DDL


def upgrade() -> None:
    for statement in _ddl():
        op.execute(statement)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_entity_match_proposals_org_identity")
    op.execute("ALTER TABLE entity_match_proposals DROP COLUMN IF EXISTS identity_key")
