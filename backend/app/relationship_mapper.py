"""Relationship mapper for Stage 2 Knowledge Graph (T3-S13-A).

upsert_relationship() is the single entry point for all edge persistence.
Every function that creates edges — map_directly_observed() (T3) and
map_inferred_from_detectors() (T4) — routes through here rather than
inserting directly.

Natural key: (org_id, from_entity_id, to_entity_id, relationship_type).
No two rows may share this combination. Duplicate prevention is enforced
at the SQL level via the UPDATE + INSERT conditional, and at the Python
level via the explicit existence check.

Immutability contract on update path:
  confidence, inferred, and first_seen_run_id are set at creation time and
  must never be changed on an existing row. T3-S14-A and T3-S15-A read
  these values as stable facts about an edge. Updating them would silently
  corrupt downstream graph queries and LLM context.

run_count tracks how many runs have confirmed this edge — the same
significance as run_count on entity rows (T3-S12-A). A relationship seen
across many runs is a strong structural signal; treat it as first-class
data, not a counter.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from app import db
from database.models.entity_relationships import (
    ALL_ENTITY_RELATIONSHIPS_DDL,
    EntityRelationship,
)

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect() -> sqlite3.Connection:
    conn = db.connect()
    conn.row_factory = sqlite3.Row
    return conn


def ensure_entity_relationships_table() -> None:
    """Create the entity_relationships table and indexes if they do not exist.

    Idempotent — safe to call on every app startup or before any insert.
    Uses IF NOT EXISTS so repeated calls are no-ops.
    """
    conn = _connect()
    try:
        for ddl in ALL_ENTITY_RELATIONSHIPS_DDL:
            conn.execute(ddl)
        conn.commit()
    finally:
        conn.close()


def upsert_relationship(
    org_id: str,
    from_entity_id: str,
    to_entity_id: str,
    relationship_type: str,
    confidence: float,
    inferred: bool,
    run_id: str,
    evidence: Optional[dict[str, Any]] = None,
) -> EntityRelationship:
    """Persist a directed edge, creating or updating as needed.

    Natural key: (org_id, from_entity_id, to_entity_id, relationship_type).

    INSERT path (no existing row):
      - Creates a new row with run_count=1, first_seen_run_id=run_id,
        last_seen_run_id=run_id, and all provided fields.

    UPDATE path (existing row found):
      - Sets last_seen_run_id=run_id and increments run_count by 1.
      - Updates evidence to reflect the most recent run's source context.
      - Does NOT change confidence, inferred, or first_seen_run_id — these
        are immutable after creation.

    Cross-org isolation: the lookup is always scoped to org_id. A row
    belonging to org_a is never matched when processing org_b, even if the
    entity UUID values collide.

    Args:
        org_id:           Workspace identifier. Scopes the edge lookup.
        from_entity_id:   Source entity UUID string (FK to entities.id).
        to_entity_id:     Target entity UUID string (FK to entities.id).
        relationship_type: One of owns/member_of/escalates_to/depends_on/routes_to.
        confidence:       0.9 for observed, 0.6 for inferred. Set at creation; ignored on update.
        inferred:         False for observed, True for co-firing. Set at creation; ignored on update.
        run_id:           Current discovery run ID.
        evidence:         Optional source field / rationale dict.

    Returns:
        The EntityRelationship reflecting the current DB state after the upsert.
    """
    from_str = str(from_entity_id)
    to_str = str(to_entity_id)
    evidence_json = json.dumps(evidence) if evidence is not None else None

    conn = _connect()
    try:
        existing = conn.execute(
            """
            SELECT * FROM entity_relationships
            WHERE org_id = ?
              AND from_entity_id = ?
              AND to_entity_id = ?
              AND relationship_type = ?
            """,
            (org_id, from_str, to_str, relationship_type),
        ).fetchone()

        if existing is None:
            # INSERT path: new edge.
            new_id = str(uuid4())
            conn.execute(
                """
                INSERT INTO entity_relationships (
                    id, org_id, from_entity_id, to_entity_id, relationship_type,
                    confidence, inferred, evidence, first_seen_run_id,
                    last_seen_run_id, run_count, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                """,
                (
                    new_id,
                    org_id,
                    from_str,
                    to_str,
                    relationship_type,
                    confidence,
                    int(inferred),
                    evidence_json,
                    run_id,
                    run_id,
                    _now(),
                ),
            )
            conn.commit()
        else:
            # UPDATE path: increment run_count, update last_seen and evidence.
            # confidence, inferred, and first_seen_run_id are never changed.
            conn.execute(
                """
                UPDATE entity_relationships
                SET last_seen_run_id = ?,
                    run_count        = run_count + 1,
                    evidence         = ?
                WHERE org_id = ?
                  AND from_entity_id = ?
                  AND to_entity_id = ?
                  AND relationship_type = ?
                """,
                (run_id, evidence_json, org_id, from_str, to_str, relationship_type),
            )
            conn.commit()

        row = conn.execute(
            """
            SELECT * FROM entity_relationships
            WHERE org_id = ?
              AND from_entity_id = ?
              AND to_entity_id = ?
              AND relationship_type = ?
            """,
            (org_id, from_str, to_str, relationship_type),
        ).fetchone()
        return EntityRelationship.from_db_row(dict(row))

    finally:
        conn.close()
