"""Create entities table for Stage 2 Knowledge Graph.

T3-S12-A — Entity Extraction from Ingestor Runs.
Schema is locked after T3-S12-A merges — column names and types must not
change without updating all downstream graph queries (T3-S13-A through
T3-S15-A depend on this schema).

resolution_confidence and resolution_status are mandatory columns that drive
graph quality in the relationship mapper (T3-S13-A).

Revision ID: 0003
Revises: 0002
Create Date: 2026-06-06
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS entities (
            id                   VARCHAR(36)   NOT NULL PRIMARY KEY,
            org_id               VARCHAR(64)   NOT NULL,
            entity_type          VARCHAR(32)   NOT NULL,
            canonical_name       VARCHAR(256)  NOT NULL,
            display_name         VARCHAR(256)  NOT NULL,
            source_system        VARCHAR(64)   NOT NULL,
            source_record_id     VARCHAR(256),
            resolution_confidence FLOAT        NOT NULL,
            resolution_status    VARCHAR(32)   NOT NULL,
            first_seen_run_id    VARCHAR(64)   NOT NULL,
            last_seen_run_id     VARCHAR(64)   NOT NULL,
            run_count            INTEGER       NOT NULL,
            metadata             TEXT,
            created_at           TIMESTAMP     NOT NULL,
            updated_at           TIMESTAMP     NOT NULL
        )
    """)

    # Resolution lookup — canonical name matching is the primary resolution path
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_entities_org_canonical
            ON entities (org_id, entity_type, canonical_name)
    """)
    # Run-scoped entity queries (e.g. entities seen in a given run)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_entities_org_run
            ON entities (org_id, last_seen_run_id)
    """)
    # Suppress low-frequency/service-account entities during enrichment
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_entities_org_run_count
            ON entities (org_id, run_count)
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_entities_org_run_count")
    op.execute("DROP INDEX IF EXISTS idx_entities_org_run")
    op.execute("DROP INDEX IF EXISTS idx_entities_org_canonical")
    op.execute("DROP TABLE IF EXISTS entities")
