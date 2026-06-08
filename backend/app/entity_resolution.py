"""Entity resolution for Stage 2 Knowledge Graph (T3-S12-A T2).

Conservative resolution is the correct default. A false merge corrupts every
relationship edge drawn from the incorrectly merged node. A false separation
can be cleanly fixed in a future fuzzy matching story. Sprint 12 must never
trade correctness for convenience.

Resolution priority (Section 3a):
  1. source_record_id present → confidence 1.0 (unique system ID)
  2. Single canonical match, same org   → confidence 0.8, status resolved
  3. Multiple canonical matches          → confidence 0.6, status ambiguous
     All existing matches marked ambiguous; new distinct row created (N+1).
  4. No match                            → new entity, confidence by source type

System and Process entities always get confidence 1.0 because they are derived
from stable AgentIQ identifiers (signal_source, detector_id), not user names.

Ambiguous rows must never be deleted or merged here — they feed the future
fuzzy matching story as intentional data.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional

from app import db
from database.models.entities import Entity, ENTITY_NAME_MAX_LEN

logger = logging.getLogger(__name__)

_STABLE_ENTITY_TYPES = frozenset({"system", "process"})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _truncate(value: str, max_len: int = ENTITY_NAME_MAX_LEN) -> str:
    """Bound a name to the VARCHAR(256) schema width.

    ServiceNow group names and Salesforce approval chains can exceed the column
    width; truncating here prevents a silent DB truncation or constraint error
    on long values. Applied to display_name and canonical_name before persist.
    """
    if value is None:
        return value
    return value[:max_len]


def _canonicalize(display_name: str) -> str:
    """Normalise a display name into the canonical match key.

    Lowercases, strips, and collapses internal whitespace runs so that
    "Alice Smith", "alice smith", and "Alice  Smith" all resolve to the single
    canonical key "alice smith" instead of creating duplicate entity rows. The
    (org_id, entity_type, canonical_name) index only dedupes exact matches, so
    normalisation MUST happen before both lookup and persistence.
    """
    return _truncate(" ".join(display_name.split()).lower())


def _connect() -> sqlite3.Connection:
    conn = db.connect()
    conn.row_factory = sqlite3.Row
    return conn


def _row_to_entity(row: sqlite3.Row) -> Entity:
    return Entity.from_db_row(dict(row))


def _insert_entity(conn: sqlite3.Connection, entity: Entity) -> None:
    row = entity.to_db_row()
    conn.execute(
        """
        INSERT INTO entities (
            id, org_id, entity_type, canonical_name, display_name,
            source_system, source_record_id, resolution_confidence,
            resolution_status, first_seen_run_id, last_seen_run_id,
            run_count, metadata, created_at, updated_at
        ) VALUES (
            :id, :org_id, :entity_type, :canonical_name, :display_name,
            :source_system, :source_record_id, :resolution_confidence,
            :resolution_status, :first_seen_run_id, :last_seen_run_id,
            :run_count, :metadata, :created_at, :updated_at
        )
        """,
        row,
    )


def get_entities_by_canonical(
    conn: sqlite3.Connection,
    org_id: str,
    entity_type: str,
    canonical_name: str,
) -> list[Entity]:
    """Return all entities matching (org_id, entity_type, canonical_name)."""
    rows = conn.execute(
        """
        SELECT * FROM entities
        WHERE org_id = ? AND entity_type = ? AND canonical_name = ?
        ORDER BY created_at ASC
        """,
        (org_id, entity_type, canonical_name),
    ).fetchall()
    return [_row_to_entity(r) for r in rows]


def mark_ambiguous(conn: sqlite3.Connection, entity_id: str) -> None:
    """Mark an entity row as ambiguous. Does not merge — intentional N+1."""
    conn.execute(
        "UPDATE entities SET resolution_status = 'ambiguous', updated_at = ? WHERE id = ?",
        (_now(), str(entity_id)),
    )


def update_entity_seen(
    conn: sqlite3.Connection,
    entity_id: str,
    run_id: str,
    source_system: str,
    new_confidence: Optional[float] = None,
    new_source_record_id: Optional[str] = None,
) -> None:
    """Increment run_count and update last_seen_run_id for a returning entity.

    Confidence upgrade (never downgrade): when a later, higher-quality signal
    arrives for an existing entity — e.g. an incoming record carries a
    source_record_id (confidence 1.0) where the stored row was name-based (0.8)
    — the stored resolution_confidence is raised to max(existing, incoming) and
    the source_record_id is backfilled if it was missing. The confidence is only
    ever increased here; a lower incoming confidence leaves the stored value
    untouched. Ambiguous rows are deliberately left alone by the caller (their
    0.6 confidence and 'ambiguous' status are intentional, load-bearing data).
    """
    if new_confidence is not None:
        # COALESCE so a NULL stored source_record_id is backfilled, but an
        # existing one is never overwritten. MAX guarantees confidence only
        # ever rises.
        conn.execute(
            """
            UPDATE entities
            SET last_seen_run_id    = ?,
                run_count           = run_count + 1,
                resolution_confidence = MAX(resolution_confidence, ?),
                source_record_id    = COALESCE(source_record_id, ?),
                updated_at          = ?
            WHERE id = ?
            """,
            (run_id, new_confidence, new_source_record_id, _now(), str(entity_id)),
        )
        return
    conn.execute(
        """
        UPDATE entities
        SET last_seen_run_id = ?,
            run_count        = run_count + 1,
            updated_at       = ?
        WHERE id = ?
        """,
        (run_id, _now(), str(entity_id)),
    )


def _initial_confidence(
    entity_type: str,
    source_record_id: Optional[str],
) -> float:
    """Confidence for a brand-new entity (zero candidates)."""
    if entity_type in _STABLE_ENTITY_TYPES:
        return 1.0
    if source_record_id:
        return 1.0
    return 0.8


def resolve_or_create_entity(
    *,
    org_id: str,
    entity_type: str,
    display_name: str,
    source_system: str,
    source_record_id: Optional[str] = None,
    run_id: str,
    metadata: Optional[dict[str, Any]] = None,
) -> Entity:
    """Resolve display_name to an existing entity or create a new one.

    Conservative resolution: ambiguous candidates are never merged.
    Multiple candidates → all marked ambiguous, new distinct row created.

    Args:
        org_id:           Workspace identifier — all queries scoped to this.
        entity_type:      One of person/team/project/object/process/system.
        display_name:     Original name from source. Normalised to canonical.
        source_system:    Source connector (e.g. 'jira', 'salesforce').
        source_record_id: Unique ID from source system when available.
        run_id:           Current discovery run ID.
        metadata:         Optional extra fields stored as JSON.

    Returns:
        The resolved or newly created Entity (reflects DB state after commit).
    """
    canonical = _canonicalize(display_name)
    display_name = _truncate(display_name)

    conn = _connect()
    try:
        candidates = get_entities_by_canonical(conn, org_id, entity_type, canonical)

        if len(candidates) == 0:
            # Branch 1 — no match: create new entity
            confidence = _initial_confidence(entity_type, source_record_id)
            entity = Entity(
                org_id=org_id,
                entity_type=entity_type,
                canonical_name=canonical,
                display_name=display_name,
                source_system=source_system,
                source_record_id=source_record_id,
                resolution_confidence=confidence,
                resolution_status="resolved",
                first_seen_run_id=run_id,
                last_seen_run_id=run_id,
                run_count=1,
                metadata=metadata,
            )
            _insert_entity(conn, entity)
            conn.commit()
            return entity

        if len(candidates) == 1:
            # Branch 2 — single candidate: update seen, return existing row.
            # Confidence is upgraded (never downgraded) when this run carries a
            # higher-quality signal — e.g. a source_record_id (1.0) for a row
            # first created name-based (0.8). Ambiguous rows are left untouched:
            # their 0.6/'ambiguous' state is intentional and must not be revised
            # by a single later sighting.
            existing = candidates[0]
            if existing.resolution_status == "ambiguous":
                update_entity_seen(conn, str(existing.id), run_id, source_system)
            else:
                incoming_confidence = _initial_confidence(entity_type, source_record_id)
                update_entity_seen(
                    conn,
                    str(existing.id),
                    run_id,
                    source_system,
                    new_confidence=incoming_confidence,
                    new_source_record_id=source_record_id,
                )
            conn.commit()
            # Return refreshed view
            row = conn.execute(
                "SELECT * FROM entities WHERE id = ?", (str(existing.id),)
            ).fetchone()
            return _row_to_entity(row)

        # Branch 3 — multiple candidates: ambiguous, create new distinct row.
        # Never merge. Mark all existing matches ambiguous.
        for candidate in candidates:
            mark_ambiguous(conn, str(candidate.id))

        entity = Entity(
            org_id=org_id,
            entity_type=entity_type,
            canonical_name=canonical,
            display_name=display_name,
            source_system=source_system,
            source_record_id=source_record_id,
            resolution_confidence=0.6,
            resolution_status="ambiguous",
            first_seen_run_id=run_id,
            last_seen_run_id=run_id,
            run_count=1,
            metadata=metadata,
        )
        _insert_entity(conn, entity)
        conn.commit()
        return entity

    finally:
        conn.close()
