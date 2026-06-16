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
    #
    # PostgreSQL port (AT-288 / Fix 1): SQLite's implicit ``rowid`` has no
    # PostgreSQL equivalent. The surviving (oldest) row per natural key is chosen
    # by ``(created_at, id)`` ascending; "most recent duplicate" is the same key
    # descending. ``array_agg(... ORDER BY ...)[1]`` takes the latest value, and
    # MAX(run_count) the strongest count — all computed over every duplicate
    # BEFORE the delete step removes them.
    op.execute(
        """
        UPDATE entity_relationships er
        SET run_count        = agg.max_run_count,
            last_seen_run_id = agg.latest_last_seen_run_id,
            evidence         = agg.latest_evidence
        FROM (
            SELECT
                org_id, from_entity_id, to_entity_id, relationship_type,
                MAX(run_count) AS max_run_count,
                (array_agg(last_seen_run_id ORDER BY created_at DESC, id DESC))[1]
                    AS latest_last_seen_run_id,
                (array_agg(evidence ORDER BY created_at DESC, id DESC))[1]
                    AS latest_evidence
            FROM entity_relationships
            GROUP BY org_id, from_entity_id, to_entity_id, relationship_type
        ) AS agg
        WHERE er.org_id = agg.org_id
          AND er.from_entity_id = agg.from_entity_id
          AND er.to_entity_id = agg.to_entity_id
          AND er.relationship_type = agg.relationship_type
          AND er.id = (
              SELECT survivor.id
              FROM entity_relationships AS survivor
              WHERE survivor.org_id = er.org_id
                AND survivor.from_entity_id = er.from_entity_id
                AND survivor.to_entity_id = er.to_entity_id
                AND survivor.relationship_type = er.relationship_type
              ORDER BY survivor.created_at ASC, survivor.id ASC
              LIMIT 1
          )
        """
    )
    op.execute(
        """
        DELETE FROM entity_relationships er
        WHERE er.id <> (
            SELECT survivor.id
            FROM entity_relationships AS survivor
            WHERE survivor.org_id = er.org_id
              AND survivor.from_entity_id = er.from_entity_id
              AND survivor.to_entity_id = er.to_entity_id
              AND survivor.relationship_type = er.relationship_type
            ORDER BY survivor.created_at ASC, survivor.id ASC
            LIMIT 1
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
