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
import re
from datetime import datetime, timezone
from typing import Any, Optional

from app import db
from app.provenance import EvidencePointer
from database.models.entities import Entity, ENTITY_NAME_MAX_LEN

logger = logging.getLogger(__name__)

_STABLE_ENTITY_TYPES = frozenset({"system", "process"})
_NUMERIC_ID_RE = re.compile(r"^\d{4,}$")
_COMPACT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{5,}$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# R16-B1: the source_timestamp on an entity's OBSERVED pointer is the run's
# observation time — when AgentIQ observed the source during the run — NOT the
# wall clock at resolution time. utc_now() at resolution would drift on every
# re-resolution and record processing time rather than observation time. The
# pointer is written once at entity creation (re-sightings never rewrite it), so
# stamping the run's recorded time keeps the entity's provenance stable for its
# lifetime. Cached per run so resolving many entities costs one run lookup.
_RUN_OBSERVED_AT_CACHE: dict[str, str] = {}


def _resolve_run_observed_at(run_id: str) -> str:
    """Return the run's recorded UTC observation time for an entity pointer.

    Reads the run record (``startedAt`` then ``completedAt``). Falls back to the
    current UTC time only when no run record exists (e.g. isolated unit tests) —
    never the per-resolution wall clock used previously, which made the provenance
    timestamp drift and reflect processing time instead of observation time.
    """
    if not run_id:
        return _now()
    cached = _RUN_OBSERVED_AT_CACHE.get(run_id)
    if cached is not None:
        return cached
    observed_at = _now()  # fallback: run record not found / no timestamp
    try:
        run = db.get_run(run_id)
        if isinstance(run, dict):
            ts = run.get("startedAt") or run.get("completedAt")
            if ts:
                observed_at = str(ts)
    except Exception:  # noqa: BLE001 — provenance timestamp is best-effort.
        pass
    _RUN_OBSERVED_AT_CACHE[run_id] = observed_at
    return observed_at


def _with_observed_evidence(
    metadata: Optional[dict[str, Any]],
    *,
    source_system: str,
    source_record_id: Optional[str],
    canonical_name: str,
    confidence: float,
    run_id: str,
) -> dict[str, Any]:
    """Return a copy of *metadata* carrying an OBSERVED EvidencePointer (R16-B1).

    An entity is observed directly in a source record, so origin='observed' and no
    extraction_job_id is needed. The pointer is stored under
    metadata['evidence_pointer'] — a JSON field that already exists, so no schema
    change is required (AC8).

    ``source_artifact`` is the stable ``source_record_id`` when the source
    provides one; otherwise it falls back to the canonical name so the mandatory
    spine is always populated. ``source_artifact_type`` records which it is
    ('record_id' vs 'canonical_name') so a consumer knows whether the artifact
    can be looked up in the source system — a canonical name is NOT guaranteed
    stable across resolution-algorithm changes. ``source_timestamp`` is the run's
    observation time (stable), not the resolution-time wall clock.
    """
    if source_record_id:
        source_artifact = source_record_id
        source_artifact_type = "record_id"
    else:
        source_artifact = canonical_name
        source_artifact_type = "canonical_name"
        logger.debug(
            "R16-B1: entity provenance source_artifact falls back to canonical_name "
            "%r (no stable source_record_id) for source_system=%s — not guaranteed "
            "stable across resolution changes.",
            canonical_name, source_system,
        )
    md = dict(metadata or {})
    md["evidence_pointer"] = EvidencePointer.observed(
        source_system=source_system,
        source_artifact=source_artifact,
        source_timestamp=_resolve_run_observed_at(run_id),
        confidence=confidence,
        source_artifact_type=source_artifact_type,
    ).to_dict()
    return md


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


def _looks_like_identifier(value: str) -> bool:
    """Return True for compact ID-ish labels like Salesforce IDs or case numbers."""
    text = str(value or "").strip()
    if not text or any(ch.isspace() for ch in text):
        return False
    if _NUMERIC_ID_RE.fullmatch(text):
        return True
    if _COMPACT_ID_RE.fullmatch(text) and any(ch.isdigit() for ch in text):
        return True
    return False


def _should_replace_display_name(existing_display: str, incoming_display: str) -> bool:
    """Use a new display value only when it improves an ID-looking saved value."""
    existing = str(existing_display or "").strip()
    incoming = str(incoming_display or "").strip()
    if not incoming or incoming == existing:
        return False
    existing_is_id = _looks_like_identifier(existing)
    incoming_is_id = _looks_like_identifier(incoming)
    if incoming_is_id and not existing_is_id:
        return False
    return existing_is_id and not incoming_is_id


def _connect() -> Any:
    conn = db.connect()
    return conn


def _row_to_entity(row: Any) -> Entity:
    return Entity.from_db_row(dict(row))


def _insert_entity(conn: Any, entity: Entity) -> None:
    row = entity.to_db_row()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO entities (
            id, org_id, entity_type, canonical_name, display_name,
            source_system, source_record_id, resolution_confidence,
            resolution_status, first_seen_run_id, last_seen_run_id,
            run_count, metadata, created_at, updated_at
        ) VALUES (
            %(id)s, %(org_id)s, %(entity_type)s, %(canonical_name)s, %(display_name)s,
            %(source_system)s, %(source_record_id)s, %(resolution_confidence)s,
            %(resolution_status)s, %(first_seen_run_id)s, %(last_seen_run_id)s,
            %(run_count)s, %(metadata)s, %(created_at)s, %(updated_at)s
        )
        """,
        row,
    )


def get_entities_by_canonical(
    conn: Any,
    org_id: str,
    entity_type: str,
    canonical_name: str,
) -> list[Entity]:
    """Return all entities matching (org_id, entity_type, canonical_name)."""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT * FROM entities
        WHERE org_id = %s AND entity_type = %s AND canonical_name = %s
        ORDER BY created_at ASC
        """,
        (org_id, entity_type, canonical_name),
    )
    rows = cur.fetchall()
    return [_row_to_entity(r) for r in rows]


def get_entities_by_source_record_id(
    conn: Any,
    org_id: str,
    entity_type: str,
    source_system: str,
    source_record_id: Optional[str],
) -> list[Entity]:
    """Return source-backed entities for the same external record."""
    if not source_record_id:
        return []
    cur = conn.cursor()
    cur.execute(
        """
        SELECT * FROM entities
        WHERE org_id = %s
          AND entity_type = %s
          AND source_system = %s
          AND source_record_id = %s
        ORDER BY created_at ASC
        """,
        (org_id, entity_type, source_system, source_record_id),
    )
    rows = cur.fetchall()
    return [_row_to_entity(r) for r in rows]


def mark_ambiguous(conn: Any, entity_id: str) -> None:
    """Mark an entity row as ambiguous. Does not merge — intentional N+1."""
    cur = conn.cursor()
    cur.execute(
        "UPDATE entities SET resolution_status = 'ambiguous', updated_at = %s WHERE id = %s",
        (_now(), str(entity_id)),
    )


def update_entity_seen(
    conn: Any,
    entity_id: str,
    run_id: str,
    source_system: str,
    new_confidence: Optional[float] = None,
    new_source_record_id: Optional[str] = None,
) -> None:
    """Update a returning entity and count at most one sighting per run.

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
        cur = conn.cursor()
        # run_count increments at most once per run: the CASE compares the OLD
        # last_seen_run_id (PostgreSQL evaluates SET right-hand sides against the
        # pre-UPDATE row) to the current run_id. The second %s is that run_id —
        # this is also why the params list carries run_id twice.
        cur.execute(
            """
            UPDATE entities
            SET last_seen_run_id    = %s,
                run_count           = run_count + CASE WHEN last_seen_run_id <> %s THEN 1 ELSE 0 END,
                resolution_confidence = GREATEST(resolution_confidence, %s),
                source_record_id    = COALESCE(source_record_id, %s),
                updated_at          = %s
            WHERE id = %s
            """,
            (
                run_id,
                run_id,
                new_confidence,
                new_source_record_id,
                _now(),
                str(entity_id),
            ),
        )
        return
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE entities
        SET last_seen_run_id = %s,
            run_count        = run_count + CASE WHEN last_seen_run_id <> %s THEN 1 ELSE 0 END,
            updated_at       = %s
        WHERE id = %s
        """,
        (run_id, run_id, _now(), str(entity_id)),
    )


def update_entity_display_and_seen(
    conn: Any,
    entity_id: str,
    *,
    run_id: str,
    display_name: str,
    canonical_name: str,
    new_confidence: float,
    new_source_record_id: Optional[str],
) -> None:
    """Refresh an existing source-backed row when a better name arrives later."""
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE entities
        SET display_name          = %s,
            canonical_name        = %s,
            last_seen_run_id      = %s,
            run_count             = run_count + CASE WHEN last_seen_run_id <> %s THEN 1 ELSE 0 END,
            resolution_confidence = GREATEST(resolution_confidence, %s),
            source_record_id      = COALESCE(source_record_id, %s),
            updated_at            = %s
        WHERE id = %s
        """,
        (
            display_name,
            canonical_name,
            run_id,
            run_id,
            new_confidence,
            new_source_record_id,
            _now(),
            str(entity_id),
        ),
    )


def _refresh_source_matches(
    conn: Any,
    matches: list[Entity],
    *,
    run_id: str,
    display_name: str,
    canonical_name: str,
    entity_type: str,
    source_record_id: Optional[str],
) -> Optional[Entity]:
    """Update same-source rows and return the first refreshed resolved entity."""
    resolved_matches = [m for m in matches if m.resolution_status == "resolved"]
    if not resolved_matches:
        return None
    incoming_confidence = _initial_confidence(entity_type, source_record_id)
    for match in resolved_matches:
        if _should_replace_display_name(match.display_name, display_name):
            update_entity_display_and_seen(
                conn,
                str(match.id),
                run_id=run_id,
                display_name=display_name,
                canonical_name=canonical_name,
                new_confidence=incoming_confidence,
                new_source_record_id=source_record_id,
            )
        else:
            update_entity_seen(
                conn,
                str(match.id),
                run_id,
                match.source_system,
                new_confidence=incoming_confidence,
                new_source_record_id=source_record_id,
            )
    cur = conn.cursor()
    cur.execute("SELECT * FROM entities WHERE id = %s", (str(resolved_matches[0].id),))
    row = cur.fetchone()
    return _row_to_entity(row)


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
            source_matches = get_entities_by_source_record_id(
                conn,
                org_id,
                entity_type,
                source_system,
                source_record_id,
            )
            source_entity = _refresh_source_matches(
                conn,
                source_matches,
                run_id=run_id,
                display_name=display_name,
                canonical_name=canonical,
                entity_type=entity_type,
                source_record_id=source_record_id,
            )
            if source_entity is not None:
                conn.commit()
                return source_entity

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
                metadata=_with_observed_evidence(
                    metadata,
                    source_system=source_system,
                    source_record_id=source_record_id,
                    canonical_name=canonical,
                    confidence=confidence,
                    run_id=run_id,
                ),
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
            cur = conn.cursor()
            cur.execute(
                "SELECT * FROM entities WHERE id = %s", (str(existing.id),)
            )
            row = cur.fetchone()
            return _row_to_entity(row)

        source_matches = [
            candidate
            for candidate in candidates
            if source_record_id
            and candidate.source_system == source_system
            and candidate.source_record_id == source_record_id
        ]
        source_entity = _refresh_source_matches(
            conn,
            source_matches,
            run_id=run_id,
            display_name=display_name,
            canonical_name=canonical,
            entity_type=entity_type,
            source_record_id=source_record_id,
        )
        if source_entity is not None:
            conn.commit()
            return source_entity

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
            metadata=_with_observed_evidence(
                metadata,
                source_system=source_system,
                source_record_id=source_record_id,
                canonical_name=canonical,
                confidence=0.6,
                run_id=run_id,
            ),
        )
        _insert_entity(conn, entity)
        conn.commit()
        return entity

    finally:
        conn.close()
