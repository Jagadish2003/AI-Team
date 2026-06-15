"""Enforce one row per entity relationship natural key.

Revision ID: 0008
Revises: 0007
Create Date: 2026-06-15
"""
from typing import Sequence, Union

from alembic import op


revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Older deployments relied on application-level upserts. Keep one row for
    # each exact edge before adding the database-level uniqueness guarantee.
    # Preserve the strongest count and the most recently inserted duplicate's
    # last-seen/evidence values on the oldest surviving row.
    op.execute(
        """
        UPDATE entity_relationships
        SET run_count = (
                SELECT MAX(duplicate.run_count)
                FROM entity_relationships AS duplicate
                WHERE duplicate.org_id = entity_relationships.org_id
                  AND duplicate.from_entity_id = entity_relationships.from_entity_id
                  AND duplicate.to_entity_id = entity_relationships.to_entity_id
                  AND duplicate.relationship_type = entity_relationships.relationship_type
            ),
            last_seen_run_id = (
                SELECT duplicate.last_seen_run_id
                FROM entity_relationships AS duplicate
                WHERE duplicate.org_id = entity_relationships.org_id
                  AND duplicate.from_entity_id = entity_relationships.from_entity_id
                  AND duplicate.to_entity_id = entity_relationships.to_entity_id
                  AND duplicate.relationship_type = entity_relationships.relationship_type
                ORDER BY duplicate.rowid DESC
                LIMIT 1
            ),
            evidence = (
                SELECT duplicate.evidence
                FROM entity_relationships AS duplicate
                WHERE duplicate.org_id = entity_relationships.org_id
                  AND duplicate.from_entity_id = entity_relationships.from_entity_id
                  AND duplicate.to_entity_id = entity_relationships.to_entity_id
                  AND duplicate.relationship_type = entity_relationships.relationship_type
                ORDER BY duplicate.rowid DESC
                LIMIT 1
            )
        WHERE rowid IN (
            SELECT MIN(rowid)
            FROM entity_relationships
            GROUP BY org_id, from_entity_id, to_entity_id, relationship_type
        )
        """
    )
    op.execute(
        """
        DELETE FROM entity_relationships
        WHERE rowid NOT IN (
            SELECT MIN(rowid)
            FROM entity_relationships
            GROUP BY org_id, from_entity_id, to_entity_id, relationship_type
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_er_org_natural_key
        ON entity_relationships (
            org_id, from_entity_id, to_entity_id, relationship_type
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_er_org_natural_key")
