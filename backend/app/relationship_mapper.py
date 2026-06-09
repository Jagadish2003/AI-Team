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

map_directly_observed() correctness contract:
  Only entities with resolution_status='resolved' may be used as edge
  endpoints. Ambiguous endpoints are silently skipped — a relationship
  with an ambiguous endpoint is semantically meaningless and must never
  corrupt the evidence trace.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4

from app import db
from database.models.entities import Entity
from database.models.entity_relationships import (
    ALL_ENTITY_RELATIONSHIPS_DDL,
    EntityRelationship,
    OBSERVED_CONFIDENCE,
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


# ---------------------------------------------------------------------------
# Helpers shared by map_directly_observed() and map_inferred_from_detectors()
# ---------------------------------------------------------------------------

def _canonicalize(name: str) -> str:
    """Normalise a display name to the canonical match key used by entity_resolution."""
    return " ".join(name.split()).lower()


def get_resolved_entity(
    org_id: str,
    entity_type: str,
    display_name: str,
    entities: List[Entity],
) -> Optional[Entity]:
    """Look up a resolved entity from the current run's entity list.

    Returns the entity only when resolution_status='resolved'. Returns None
    when:
      - No entity with the given (entity_type, canonical_name) is in the list.
      - The matching entity has resolution_status='ambiguous' or 'unresolved'.

    This is the correctness gate for map_directly_observed(). An ambiguous
    endpoint means the relationship is semantically undefined and must be
    skipped entirely — drawing an edge to an ambiguous entity would corrupt
    the evidence trace.

    Args:
        org_id:       Workspace identifier. All entities are already scoped to
                      org_id by extract_entities(), but we guard it explicitly.
        entity_type:  person / team / project / object / process / system.
        display_name: Raw name from the ingestor field. Canonicalised here.
        entities:     The Entity list returned by extract_entities() for this run.
    """
    if not display_name or not display_name.strip():
        return None
    canonical = _canonicalize(display_name)
    for entity in entities:
        if (
            entity.org_id == org_id
            and entity.entity_type == entity_type
            and entity.canonical_name == canonical
        ):
            if entity.resolution_status == "resolved":
                return entity
            # Ambiguous or unresolved — skip. Do not return a partial match.
            return None
    return None


def _sn_ref_name(value: Any) -> Optional[str]:
    """Extract a display name from a ServiceNow reference field.

    ServiceNow reference fields can be:
      - A string (the display name directly)
      - A dict with display_value, value, or name keys
    """
    if not value:
        return None
    if isinstance(value, dict):
        for key in ("display_value", "value", "name", "Name"):
            v = value.get(key)
            if v and str(v).strip():
                return str(v).strip()
        return None
    s = str(value).strip()
    return s or None


def _sf_owner_name(record: Dict[str, Any]) -> Optional[str]:
    """Extract owner display name from a Salesforce record dict."""
    for field in ("OwnerId", "owner_id", "owner_name", "AssignedTo", "assigned_to"):
        val = record.get(field)
        if not val:
            continue
        if isinstance(val, dict):
            for key in ("display_value", "displayName", "Name", "name", "value", "id"):
                v = val.get(key)
                if v and str(v).strip():
                    return str(v).strip()
        else:
            s = str(val).strip()
            if s:
                return s
    return None


def _sf_object_name(record: Dict[str, Any]) -> Optional[str]:
    """Extract object/record identifier from a Salesforce record dict."""
    for field in ("Id", "id", "record_id", "Name", "name", "number", "CaseNumber"):
        val = record.get(field)
        if val and str(val).strip():
            return str(val).strip()
    return None


# ---------------------------------------------------------------------------
# map_directly_observed() — T3 primary deliverable
# ---------------------------------------------------------------------------

def map_directly_observed(
    org_id: str,
    run_id: str,
    ingestor_data: Dict[str, Any],
    entities: List[Entity],
) -> int:
    """Map relationships directly observable from ingestor source fields.

    All edges created here have confidence=0.9 and inferred=False. These
    values are not configurable — they are constants of graph truth for
    directly observed relationships.

    Only entities with resolution_status='resolved' are used as endpoints.
    Ambiguous entities are silently skipped via get_resolved_entity().

    Relationship types handled:
      owns        — Salesforce/nCino Person → Object via OwnerId/owner fields.
      member_of   — ServiceNow Person → Team via assigned_to/assignment_group.
      escalates_to — ServiceNow and Jira Object → Person/Team via escalation fields.

    Individual record failures are caught and logged; the function continues
    processing remaining records and never raises to the caller.

    Args:
        org_id:        Workspace identifier. All lookups scoped to this.
        run_id:        Current discovery run ID.
        ingestor_data: Dict keyed by connector name (salesforce, jira, servicenow).
        entities:      Resolved entity list from extract_entities() for this run.

    Returns:
        Number of upsert_relationship() calls made (created or updated edges).
    """
    count = 0

    # -----------------------------------------------------------------------
    # owns: Salesforce/nCino Person → Object via OwnerId / owner fields
    # -----------------------------------------------------------------------
    sf_data: Dict[str, Any] = ingestor_data.get("salesforce") or {}
    for collection in (
        "records", "sample_records", "cases", "tasks",
        "opportunities", "objects", "loans", "loan_applications",
    ):
        for record in (sf_data.get(collection) or []):
            if not isinstance(record, dict):
                continue
            try:
                owner_name = _sf_owner_name(record)
                obj_name = _sf_object_name(record)
                if not owner_name or not obj_name:
                    continue
                person = get_resolved_entity(org_id, "person", owner_name, entities)
                obj = get_resolved_entity(org_id, "object", obj_name, entities)
                if person is None or obj is None:
                    continue
                source = str(record.get("source_system") or "salesforce")
                upsert_relationship(
                    org_id=org_id,
                    from_entity_id=str(person.id),
                    to_entity_id=str(obj.id),
                    relationship_type="owns",
                    confidence=OBSERVED_CONFIDENCE,
                    inferred=False,
                    run_id=run_id,
                    evidence={"field": "OwnerId", "source": source},
                )
                count += 1
            except Exception as exc:
                logger.debug(
                    "map_directly_observed owns — record skipped in %s: %s",
                    collection, exc,
                )

    # nCino nested records
    ncino: Dict[str, Any] = sf_data.get("ncino") or {}
    for key in ("loan_applications", "loan_portfolios"):
        for record in (ncino.get(key) or []):
            if not isinstance(record, dict):
                continue
            try:
                owner_raw = record.get("OwnerId") or record.get("owner_id")
                owner_name = str(owner_raw).strip() if owner_raw else None
                obj_name = _sf_object_name(record)
                if not owner_name or not obj_name:
                    continue
                person = get_resolved_entity(org_id, "person", owner_name, entities)
                obj = get_resolved_entity(org_id, "object", obj_name, entities)
                if person is None or obj is None:
                    continue
                upsert_relationship(
                    org_id=org_id,
                    from_entity_id=str(person.id),
                    to_entity_id=str(obj.id),
                    relationship_type="owns",
                    confidence=OBSERVED_CONFIDENCE,
                    inferred=False,
                    run_id=run_id,
                    evidence={"field": "OwnerId", "source": "ncino"},
                )
                count += 1
            except Exception as exc:
                logger.debug("map_directly_observed ncino owns — record skipped: %s", exc)

    # -----------------------------------------------------------------------
    # member_of: ServiceNow Person → Team via assigned_to / assignment_group
    # -----------------------------------------------------------------------
    sn_data: Dict[str, Any] = ingestor_data.get("servicenow") or {}
    incident_metrics: Dict[str, Any] = sn_data.get("incident_metrics") or {}
    for incident in (incident_metrics.get("incidents") or []):
        if not isinstance(incident, dict):
            continue
        try:
            assigned_name = _sn_ref_name(incident.get("assigned_to"))
            group_name = _sn_ref_name(incident.get("assignment_group"))
            if not assigned_name or not group_name:
                continue
            person = get_resolved_entity(org_id, "person", assigned_name, entities)
            team = get_resolved_entity(org_id, "team", group_name, entities)
            if person is None or team is None:
                continue
            upsert_relationship(
                org_id=org_id,
                from_entity_id=str(person.id),
                to_entity_id=str(team.id),
                relationship_type="member_of",
                confidence=OBSERVED_CONFIDENCE,
                inferred=False,
                run_id=run_id,
                evidence={"field": "assignment_group", "source": "servicenow"},
            )
            count += 1
        except Exception as exc:
            logger.debug(
                "map_directly_observed member_of — incident %s skipped: %s",
                incident.get("number") or incident.get("id"),
                exc,
            )

    # -----------------------------------------------------------------------
    # escalates_to: Object → Person/Team via ServiceNow escalated_to field
    # -----------------------------------------------------------------------
    for incident in (incident_metrics.get("incidents") or []):
        if not isinstance(incident, dict):
            continue
        try:
            escalated_to = incident.get("escalated_to")
            if not escalated_to:
                continue
            inc_number = str(
                incident.get("number") or incident.get("id") or ""
            ).strip()
            esc_name = _sn_ref_name(escalated_to)
            if not inc_number or not esc_name:
                continue
            from_ent = get_resolved_entity(org_id, "object", inc_number, entities)
            # Try person first; fall back to team (escalation targets vary)
            to_ent = get_resolved_entity(
                org_id, "person", esc_name, entities
            ) or get_resolved_entity(org_id, "team", esc_name, entities)
            if from_ent is None or to_ent is None:
                continue
            upsert_relationship(
                org_id=org_id,
                from_entity_id=str(from_ent.id),
                to_entity_id=str(to_ent.id),
                relationship_type="escalates_to",
                confidence=OBSERVED_CONFIDENCE,
                inferred=False,
                run_id=run_id,
                evidence={"field": "escalated_to", "source": "servicenow"},
            )
            count += 1
        except Exception as exc:
            logger.debug(
                "map_directly_observed escalates_to SN — incident %s skipped: %s",
                incident.get("number") or incident.get("id"),
                exc,
            )

    # -----------------------------------------------------------------------
    # escalates_to: Jira Object → Person/Team via escalation label +
    #               escalated_to / escalation_target field
    # -----------------------------------------------------------------------
    jira_data: Dict[str, Any] = ingestor_data.get("jira") or {}
    issue_metrics: Dict[str, Any] = jira_data.get("issue_metrics") or {}
    for issue in (issue_metrics.get("issues") or []):
        if not isinstance(issue, dict):
            continue
        try:
            labels = issue.get("labels") or []
            has_escalation_label = any(
                str(lbl).lower() in ("escalation", "escalate", "escalated")
                for lbl in labels
            )
            if not has_escalation_label:
                continue
            esc_target_raw = issue.get("escalated_to") or issue.get("escalation_target")
            if not esc_target_raw:
                continue
            issue_key = str(issue.get("key") or issue.get("id") or "").strip()
            esc_name = _sn_ref_name(esc_target_raw)
            if not issue_key or not esc_name:
                continue
            from_ent = get_resolved_entity(org_id, "object", issue_key, entities)
            to_ent = get_resolved_entity(
                org_id, "person", esc_name, entities
            ) or get_resolved_entity(org_id, "team", esc_name, entities)
            if from_ent is None or to_ent is None:
                continue
            upsert_relationship(
                org_id=org_id,
                from_entity_id=str(from_ent.id),
                to_entity_id=str(to_ent.id),
                relationship_type="escalates_to",
                confidence=OBSERVED_CONFIDENCE,
                inferred=False,
                run_id=run_id,
                evidence={"field": "escalation_label", "source": "jira"},
            )
            count += 1
        except Exception as exc:
            logger.debug(
                "map_directly_observed escalates_to Jira — issue %s skipped: %s",
                issue.get("key") or issue.get("id"),
                exc,
            )

    logger.info(
        "map_directly_observed — run=%s org=%s edges_written=%d",
        run_id, org_id, count,
    )
    return count
