"""Relationship mapper for Stage 2 Knowledge Graph (T3-S13-A).

upsert_relationship() is the single entry point for all edge persistence.
Every function that creates edges — map_directly_observed() (T3) and
map_inferred_from_detectors() (T4) — routes through here rather than
inserting directly.

Natural key: (org_id, from_entity_id, to_entity_id, relationship_type).
No two rows may share this combination. Duplicate prevention is enforced
by a database unique index and an atomic SQL upsert.

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
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4

from app import db
from database.models.entities import Entity
from database.models.entity_relationships import (
    ALL_ENTITY_RELATIONSHIPS_DDL,
    EntityRelationship,
    INFERRED_CONFIDENCE,
    OBSERVED_CONFIDENCE,
    RELATIONSHIP_TYPES,
)
from app.provenance import EvidencePointer

try:
    from discovery.detectors.checklist_bottleneck import DETECTOR_ID as CHECKLIST_BOTTLENECK_DETECTOR_ID
    from discovery.detectors.covenant_tracking_gap import DETECTOR_ID as COVENANT_TRACKING_DETECTOR_ID
    from discovery.detectors.db_sla_breach_rate import DETECTOR_ID as DB_SLA_BREACH_RATE_DETECTOR_ID
    from discovery.detectors.disbursement_overdue import DETECTOR_ID as DISBURSEMENT_OVERDUE_DETECTOR_ID
    from discovery.detectors.loan_origination_routing_friction import DETECTOR_ID as LOAN_ORIGINATION_DETECTOR_ID
    from discovery.detectors.repetition import DETECTOR_ID as REPETITIVE_AUTOMATION_DETECTOR_ID
    from discovery.detectors.handoff_friction import DETECTOR_ID as HANDOFF_FRICTION_DETECTOR_ID
    from discovery.detectors.approval_bottleneck import DETECTOR_ID as APPROVAL_BOTTLENECK_DETECTOR_ID
    from discovery.detectors.knowledge_gap import DETECTOR_ID as KNOWLEDGE_GAP_DETECTOR_ID
    from discovery.detectors.integration_concentration import DETECTOR_ID as INTEGRATION_CONCENTRATION_DETECTOR_ID
    from discovery.detectors.cross_system_echo import DETECTOR_ID as CROSS_SYSTEM_ECHO_DETECTOR_ID
except ModuleNotFoundError:  # project-root execution uses backend as package
    from backend.discovery.detectors.checklist_bottleneck import DETECTOR_ID as CHECKLIST_BOTTLENECK_DETECTOR_ID
    from backend.discovery.detectors.covenant_tracking_gap import DETECTOR_ID as COVENANT_TRACKING_DETECTOR_ID
    from backend.discovery.detectors.db_sla_breach_rate import DETECTOR_ID as DB_SLA_BREACH_RATE_DETECTOR_ID
    from backend.discovery.detectors.disbursement_overdue import DETECTOR_ID as DISBURSEMENT_OVERDUE_DETECTOR_ID
    from backend.discovery.detectors.loan_origination_routing_friction import DETECTOR_ID as LOAN_ORIGINATION_DETECTOR_ID
    from backend.discovery.detectors.repetition import DETECTOR_ID as REPETITIVE_AUTOMATION_DETECTOR_ID
    from backend.discovery.detectors.handoff_friction import DETECTOR_ID as HANDOFF_FRICTION_DETECTOR_ID
    from backend.discovery.detectors.approval_bottleneck import DETECTOR_ID as APPROVAL_BOTTLENECK_DETECTOR_ID
    from backend.discovery.detectors.knowledge_gap import DETECTOR_ID as KNOWLEDGE_GAP_DETECTOR_ID
    from backend.discovery.detectors.integration_concentration import DETECTOR_ID as INTEGRATION_CONCENTRATION_DETECTOR_ID
    from backend.discovery.detectors.cross_system_echo import DETECTOR_ID as CROSS_SYSTEM_ECHO_DETECTOR_ID

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect() -> Any:
    return db.connect()


def ensure_entity_relationships_table() -> None:
    """No-op. The entity_relationships table is provisioned externally.

    Created by database/provision/provision.sh; the application no longer
    creates this table at runtime.
    """
    return None


def _relationship_pointer(
    *,
    inferred: bool,
    run_id: str,
    evidence: dict[str, Any],
    from_id: str,
    to_id: str,
    relationship_type: str,
    confidence: float,
) -> EvidencePointer:
    """Build the EvidencePointer for an edge from what the mapper already knows.

    Observed edges carry the source system/field they were read from; inferred
    (co-firing) edges name the run as their extraction job and reference the
    detectors that produced the inference. Callers that supply richer provenance
    in the evidence dict (source / source_artifact / source_timestamp) have it
    honoured; otherwise sensible, stable fallbacks are used.
    """
    source_system = evidence.get("source") or "agentiq"
    source_artifact = (
        evidence.get("source_artifact")
        or evidence.get("field")
        or "+".join(evidence.get("detector_ids") or [])
        or f"{from_id}|{relationship_type}|{to_id}"
    )
    source_timestamp = evidence.get("source_timestamp")
    if inferred:
        return EvidencePointer.inferred(
            source_system=source_system,
            source_artifact=source_artifact,
            extraction_job_id=run_id,
            source_timestamp=source_timestamp,
            confidence=confidence,
        )
    return EvidencePointer.observed(
        source_system=source_system,
        source_artifact=source_artifact,
        source_timestamp=source_timestamp,
        confidence=confidence,
    )


def upsert_relationship(
    org_id: str,
    from_entity_id: str,
    to_entity_id: str,
    relationship_type: str,
    confidence: float,
    inferred: bool,
    run_id: str,
    evidence: Optional[dict[str, Any]] = None,
) -> Optional[EntityRelationship]:
    """Persist a directed edge, creating or updating as needed.

    Natural key: (org_id, from_entity_id, to_entity_id, relationship_type).

    INSERT path (no existing row):
      - Creates a new row with run_count=1, first_seen_run_id=run_id,
        last_seen_run_id=run_id, and all provided fields.

    UPDATE path (existing row found):
      - Sets last_seen_run_id=run_id and increments run_count only when this is
        the first sighting of the edge in that discovery run.
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
    # PII guard: mapper-owned evidence stores field/source names, detector IDs,
    # and rationale only. Do not pass raw display names, case titles, amounts,
    # or external record values into this generic persistence helper.
    #
    # R16-B1: every edge carries an EvidencePointer. Observed edges => origin
    # 'observed' (no job id); inferred (co-firing) edges => origin 'inferred' and
    # MUST name the run as their extraction job. An inferred edge whose pointer is
    # invalid (no extraction_job_id) is refused here — inferred content must never
    # be persisted as if it were directly observed truth (AC2).
    evidence_with_pointer = dict(evidence) if evidence else {}
    pointer = _relationship_pointer(
        inferred=inferred,
        run_id=run_id,
        evidence=evidence_with_pointer,
        from_id=from_str,
        to_id=to_str,
        relationship_type=relationship_type,
        confidence=confidence,
    )
    if not pointer.is_valid():
        logger.error(
            "Refusing to persist %s edge %s -> %s: invalid provenance pointer "
            "(inferred edges require an extraction_job_id)",
            relationship_type,
            from_str,
            to_str,
        )
        return None
    evidence_with_pointer["evidence_pointer"] = pointer.to_dict()
    evidence_json = json.dumps(evidence_with_pointer)

    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT * FROM entity_relationships
            WHERE org_id = %s
              AND from_entity_id = %s
              AND to_entity_id = %s
              AND relationship_type = %s
            """,
            (org_id, from_str, to_str, relationship_type),
        )
        existing = cur.fetchone()

        if existing is None:
            # INSERT path: new edge.
            new_id = str(uuid4())
            cur.execute(
                """
                INSERT INTO entity_relationships (
                    id, org_id, from_entity_id, to_entity_id, relationship_type,
                    confidence, inferred, evidence, first_seen_run_id,
                    last_seen_run_id, run_count, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 1, %s)
                """,
                (
                    new_id,
                    org_id,
                    from_str,
                    to_str,
                    relationship_type,
                    confidence,
                    bool(inferred),
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
            stored_confidence = float(existing["confidence"])
            if abs(stored_confidence - float(confidence)) > 1e-9:
                logger.debug(
                    "upsert_relationship confidence mismatch ignored: org_id=%s "
                    "relationship_type=%s stored_confidence=%.3f incoming_confidence=%.3f",
                    org_id,
                    relationship_type,
                    stored_confidence,
                    float(confidence),
                )
            if run_id == existing["last_seen_run_id"]:
                # Already counted for this run — refresh evidence only, do NOT
                # increment run_count (run_count tracks distinct confirming runs,
                # not call count). See the "first sighting in that run" contract
                # in this function's docstring.
                cur.execute(
                    """
                    UPDATE entity_relationships
                    SET evidence = %s
                    WHERE org_id = %s
                      AND from_entity_id = %s
                      AND to_entity_id = %s
                      AND relationship_type = %s
                    """,
                    (evidence_json, org_id, from_str, to_str, relationship_type),
                )
            else:
                cur.execute(
                    """
                    UPDATE entity_relationships
                    SET last_seen_run_id = %s,
                        run_count        = run_count + 1,
                        evidence         = %s
                    WHERE org_id = %s
                      AND from_entity_id = %s
                      AND to_entity_id = %s
                      AND relationship_type = %s
                    """,
                    (run_id, evidence_json, org_id, from_str, to_str, relationship_type),
                )
            conn.commit()

        cur.execute(
            """
            SELECT * FROM entity_relationships
            WHERE org_id = %s
              AND from_entity_id = %s
              AND to_entity_id = %s
              AND relationship_type = %s
            """,
            (org_id, from_str, to_str, relationship_type),
        )
        row = cur.fetchone()
        return EntityRelationship.from_db_row(dict(row))

    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Helpers shared by map_directly_observed() and map_inferred_from_detectors()
# ---------------------------------------------------------------------------

def _canonicalize(name: str) -> str:
    """Normalise a display name to the canonical match key used by entity_resolution."""
    return " ".join(name.split()).lower()


def _entity_matches_ref(
    entity: Entity,
    org_id: str,
    entity_type: str,
    display_name: str,
) -> bool:
    """Return True when an entity matches a raw source ref.

    Stage 13 observed edges often carry stable IDs (OwnerId, record Id) while
    the entity's display_name may be a friendly label. Matching source_record_id
    keeps those source-backed edges from being dropped.
    """
    if entity.org_id != org_id or entity.entity_type != entity_type:
        return False
    ref = (display_name or "").strip()
    if not ref:
        return False
    canonical = _canonicalize(ref)
    if entity.canonical_name == canonical:
        return True
    if (entity.display_name or "").strip().lower() == ref.lower():
        return True
    source_record_id = getattr(entity, "source_record_id", None)
    return bool(source_record_id and str(source_record_id).strip().lower() == ref.lower())


def _is_entity_ambiguous(
    org_id: str,
    entity_type: str,
    display_name: str,
    entities: List[Entity],
) -> bool:
    """Return True if the entity exists in the list but has resolution_status='ambiguous'.

    Used by map_directly_observed() to distinguish "entity not found" (no skip
    credit) from "entity found but ambiguous" (increments skipped_ambiguous_count
    in the telemetry payload). Never raises — a broken entity list returns False.
    """
    if not display_name or not display_name.strip():
        return False
    for entity in entities:
        if _entity_matches_ref(entity, org_id, entity_type, display_name):
            return entity.resolution_status == "ambiguous"
    return False


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
    for entity in entities:
        if _entity_matches_ref(entity, org_id, entity_type, display_name):
            if entity.resolution_status == "resolved":
                return entity
            # Ambiguous or unresolved — skip. Do not return a partial match.
            return None
    return None


def get_resolved_source_entity(
    org_id: str,
    entity_type: str,
    source_system: str,
    source_record_id: str,
    entities: List[Entity],
) -> Optional[Entity]:
    """Look up an exact resolved source identity within the current org."""
    stable_id = str(source_record_id or "").strip()
    if not stable_id:
        return None
    for entity in entities:
        if (
            entity.org_id == org_id
            and entity.entity_type == entity_type
            and entity.source_system == source_system
            and str(entity.source_record_id or "").strip() == stable_id
        ):
            return entity if entity.resolution_status == "resolved" else None
    return None


def _remove_servicenow_relationship_source(
    *,
    org_id: str,
    relationship_sys_id: str,
    keep_key: Optional[tuple[str, str, str]] = None,
) -> int:
    """Remove graph edges for one explicit ServiceNow relationship record.

    The evidence match and delete are both organization-scoped. ``keep_key``
    lets a changed relationship retain its current natural key while removing
    a prior direction/type/end-point representation of the same source row.
    """
    conn = _connect()
    removed = 0
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, from_entity_id, to_entity_id, relationship_type, evidence
            FROM entity_relationships
            WHERE org_id = %s AND inferred = %s
            """,
            (org_id, False),
        )
        for row in cur.fetchall():
            evidence = row["evidence"]
            if isinstance(evidence, str):
                try:
                    evidence = json.loads(evidence)
                except (TypeError, ValueError):
                    continue
            if not isinstance(evidence, dict):
                continue
            source_id = str(
                evidence.get("relationship_sys_id")
                or evidence.get("source_record_id")
                or ""
            ).strip()
            if evidence.get("source") != "servicenow" or source_id != relationship_sys_id:
                continue
            natural_key = (
                str(row["from_entity_id"]),
                str(row["to_entity_id"]),
                str(row["relationship_type"]),
            )
            if keep_key is not None and natural_key == keep_key:
                continue
            cur.execute(
                "DELETE FROM entity_relationships WHERE id = %s AND org_id = %s",
                (row["id"], org_id),
            )
            removed += 1
        conn.commit()
        return removed
    finally:
        conn.close()


def list_servicenow_relationship_source_ids(org_id: str) -> set[str]:
    """List explicit ServiceNow edge ids already admitted for one org."""
    conn = _connect()
    source_ids: set[str] = set()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT evidence FROM entity_relationships WHERE org_id = %s AND inferred = %s",
            (org_id, False),
        )
        for row in cur.fetchall():
            evidence = row["evidence"]
            if isinstance(evidence, str):
                try:
                    evidence = json.loads(evidence)
                except (TypeError, ValueError):
                    continue
            if not isinstance(evidence, dict) or evidence.get("source") != "servicenow":
                continue
            if (
                evidence.get("field") != "cmdb_rel_ci"
                and evidence.get("source_type") != "servicenow_cmdb_rel_ci"
            ):
                continue
            source_id = str(
                evidence.get("relationship_sys_id")
                or evidence.get("source_record_id")
                or ""
            ).strip()
            if source_id:
                source_ids.add(source_id)
        return source_ids
    finally:
        conn.close()


def list_servicenow_cmdb_relationships(org_id: str) -> List[Dict[str, Any]]:
    """Return active observed CMDB edges with their source provenance.

    Both relationship and endpoint lookups are constrained to ``org_id``.
    This lets workflow-signal resolvers traverse relationships that were
    confirmed on an earlier incremental run without treating registry or
    topology data as a new source observation.
    """
    conn = _connect()
    relationships: List[Dict[str, Any]] = []
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT er.relationship_type, er.evidence,
                   source.source_record_id AS source_ci_id,
                   target.source_record_id AS target_ci_id
            FROM entity_relationships er
            JOIN entities source
              ON source.id = er.from_entity_id AND source.org_id = er.org_id
            JOIN entities target
              ON target.id = er.to_entity_id AND target.org_id = er.org_id
            WHERE er.org_id = %s
              AND er.inferred = %s
              AND source.entity_type = 'system'
              AND target.entity_type = 'system'
              AND source.source_system = 'servicenow'
              AND target.source_system = 'servicenow'
              AND source.resolution_status = 'resolved'
              AND target.resolution_status = 'resolved'
            """,
            (org_id, False),
        )
        for row in cur.fetchall():
            evidence = row["evidence"]
            if isinstance(evidence, str):
                try:
                    evidence = json.loads(evidence)
                except (TypeError, ValueError):
                    continue
            if not isinstance(evidence, dict) or evidence.get("source") != "servicenow":
                continue
            if (
                evidence.get("field") != "cmdb_rel_ci"
                and evidence.get("source_type") != "servicenow_cmdb_rel_ci"
            ):
                continue
            relationship_sys_id = str(
                evidence.get("relationship_sys_id")
                or evidence.get("source_record_id")
                or ""
            ).strip()
            source_ci_id = str(row["source_ci_id"] or "").strip()
            target_ci_id = str(row["target_ci_id"] or "").strip()
            if not relationship_sys_id or not source_ci_id or not target_ci_id:
                continue
            relationships.append(
                {
                    "sys_id": relationship_sys_id,
                    "relationship_type": str(row["relationship_type"]),
                    "source_ci_id": source_ci_id,
                    "target_ci_id": target_ci_id,
                    "source_type": evidence.get("source_type"),
                    "source_timestamp": evidence.get("source_timestamp"),
                    "source_url": evidence.get("source_url"),
                    "origin": "observed",
                }
            )
        return sorted(
            relationships,
            key=lambda relationship: (
                relationship["relationship_type"],
                relationship["source_ci_id"],
                relationship["target_ci_id"],
                relationship["sys_id"],
            ),
        )
    finally:
        conn.close()


def apply_servicenow_cmdb_relationship_delta(
    *,
    org_id: str,
    run_id: str,
    relationships: List[Dict[str, Any]],
    entities: List[Entity],
) -> int:
    """Apply explicit changed or deleted ``cmdb_rel_ci`` records.

    A tombstone removes only the observed edge carrying that ServiceNow record
    id. An active row is upserted and any older representation of that same
    source relationship is removed. No edge is inferred from topology or names.
    """
    count = 0
    for relationship in relationships:
        if not isinstance(relationship, dict):
            continue
        relationship_sys_id = str(
            relationship.get("sys_id") or relationship.get("artifact_id") or ""
        ).strip()
        if not relationship_sys_id:
            continue
        if relationship.get("change_kind") == "deleted":
            _remove_servicenow_relationship_source(
                org_id=org_id,
                relationship_sys_id=relationship_sys_id,
            )
            count += 1
            continue

        source_ci_id = str(relationship.get("source_ci_id") or "").strip()
        target_ci_id = str(relationship.get("target_ci_id") or "").strip()
        relationship_type = str(relationship.get("relationship_type") or "").strip()
        if relationship_type not in RELATIONSHIP_TYPES:
            continue
        source_entity = get_resolved_source_entity(
            org_id, "system", "servicenow", source_ci_id, entities
        )
        target_entity = get_resolved_source_entity(
            org_id, "system", "servicenow", target_ci_id, entities
        )
        if source_entity is None or target_entity is None:
            continue
        natural_key = (
            str(source_entity.id),
            str(target_entity.id),
            relationship_type,
        )
        _remove_servicenow_relationship_source(
            org_id=org_id,
            relationship_sys_id=relationship_sys_id,
            keep_key=natural_key,
        )
        persisted = upsert_relationship(
            org_id=org_id,
            from_entity_id=natural_key[0],
            to_entity_id=natural_key[1],
            relationship_type=relationship_type,
            confidence=OBSERVED_CONFIDENCE,
            inferred=False,
            run_id=run_id,
            evidence={
                "field": "cmdb_rel_ci",
                "source": "servicenow",
                "source_artifact": relationship_sys_id,
                "source_record_id": relationship_sys_id,
                "source_type": relationship.get("source_type"),
                "source_timestamp": relationship.get("source_timestamp"),
                "source_url": relationship.get("source_url"),
                "relationship_sys_id": relationship_sys_id,
            },
        )
        if persisted is not None:
            count += 1
    return count


def _sn_ref_name(value: Any) -> Optional[str]:
    """Extract a display name from a ServiceNow reference field.

    ServiceNow reference fields can be:
      - A string (the display name directly)
      - A dict with display_value, value, or name keys
    """
    if not value:
        return None
    if isinstance(value, list):
        return _sn_ref_name(value[0]) if value else None
    if isinstance(value, dict):
        for key in ("display_value", "displayValue", "displayName", "value", "name", "Name"):
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
    _counters: Optional[Dict[str, int]] = None,
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
      CMDB graph verbs — ServiceNow System → System via explicit cmdb_rel_ci rows.

    Individual record failures are caught and logged; the function continues
    processing remaining records and never raises to the caller.

    Args:
        org_id:        Workspace identifier. All lookups scoped to this.
        run_id:        Current discovery run ID.
        ingestor_data: Dict keyed by connector name (salesforce, jira, servicenow).
        entities:      Resolved entity list from extract_entities() for this run.
        _counters:     Optional mutable dict. When provided, the key
                       'skipped_ambiguous' is incremented each time an edge is
                       skipped because one endpoint had resolution_status='ambiguous'.
                       Callers that do not need this metric omit the argument.

    Returns:
        Number of unique edges created or updated.
    """
    count = 0
    seen_edges: set[tuple[str, str, str]] = set()

    def write_once(
        from_entity: Entity,
        to_entity: Entity,
        relationship_type: str,
        evidence: Dict[str, Any],
    ) -> bool:
        edge_key = (
            str(from_entity.id),
            relationship_type,
            str(to_entity.id),
        )
        if edge_key in seen_edges:
            return False
        upsert_relationship(
            org_id=org_id,
            from_entity_id=edge_key[0],
            to_entity_id=edge_key[2],
            relationship_type=relationship_type,
            confidence=OBSERVED_CONFIDENCE,
            inferred=False,
            run_id=run_id,
            evidence=evidence,
        )
        seen_edges.add(edge_key)
        return True

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
                    if _counters is not None and (
                        _is_entity_ambiguous(org_id, "person", owner_name, entities)
                        or _is_entity_ambiguous(org_id, "object", obj_name, entities)
                    ):
                        _counters["skipped_ambiguous"] = _counters.get("skipped_ambiguous", 0) + 1
                    continue
                source = str(record.get("source_system") or "salesforce")
                if write_once(
                    person,
                    obj,
                    "owns",
                    {"field": "OwnerId", "source": source},
                ):
                    count += 1
            except Exception as exc:
                logger.debug(
                    "map_directly_observed owns — record skipped in %s: %s",
                    collection, exc,
                )

    # nCino nested records
    ncino: Dict[str, Any] = sf_data.get("ncino") or {}
    for key in ("loans", "loan_applications", "loan_portfolios"):
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
                    if _counters is not None and (
                        _is_entity_ambiguous(org_id, "person", owner_name, entities)
                        or _is_entity_ambiguous(org_id, "object", obj_name, entities)
                    ):
                        _counters["skipped_ambiguous"] = _counters.get("skipped_ambiguous", 0) + 1
                    continue
                if write_once(
                    person,
                    obj,
                    "owns",
                    {"field": "OwnerId", "source": "ncino"},
                ):
                    count += 1
            except Exception as exc:
                logger.debug("map_directly_observed ncino owns — record skipped: %s", exc)

    # -----------------------------------------------------------------------
    # ServiceNow CMDB: System -> System, explicitly observed in cmdb_rel_ci.
    # -----------------------------------------------------------------------
    sn_data: Dict[str, Any] = ingestor_data.get("servicenow") or {}
    cmdb_data: Dict[str, Any] = sn_data.get("cmdb") or {}
    payload_org = str(cmdb_data.get("org_id") or "").strip()
    if payload_org and payload_org != org_id:
        raise ValueError(
            f"ServiceNow CMDB payload org {payload_org!r} does not match {org_id!r}"
        )
    for relationship in cmdb_data.get("relationships", []) or []:
        if not isinstance(relationship, dict):
            continue
        try:
            source_ci_id = str(relationship.get("source_ci_id") or "").strip()
            target_ci_id = str(relationship.get("target_ci_id") or "").strip()
            relationship_type = str(
                relationship.get("relationship_type") or ""
            ).strip()
            relationship_sys_id = str(relationship.get("sys_id") or "").strip()
            if (
                not source_ci_id
                or not target_ci_id
                or not relationship_sys_id
                or relationship_type not in RELATIONSHIP_TYPES
            ):
                continue
            source_entity = get_resolved_source_entity(
                org_id,
                "system",
                "servicenow",
                source_ci_id,
                entities,
            )
            target_entity = get_resolved_source_entity(
                org_id,
                "system",
                "servicenow",
                target_ci_id,
                entities,
            )
            if source_entity is None or target_entity is None:
                continue
            if write_once(
                source_entity,
                target_entity,
                relationship_type,
                {
                    "field": "cmdb_rel_ci",
                    "source": "servicenow",
                    "source_artifact": relationship_sys_id,
                    "source_record_id": relationship_sys_id,
                    "source_type": relationship.get("source_type"),
                    "source_timestamp": relationship.get("source_timestamp"),
                    "source_url": relationship.get("source_url"),
                    "relationship_sys_id": relationship_sys_id,
                },
            ):
                count += 1
        except Exception as exc:
            logger.debug(
                "map_directly_observed CMDB relationship %s skipped: %s",
                relationship.get("sys_id"),
                exc,
            )

    # -----------------------------------------------------------------------
    # member_of: ServiceNow Person -> Team via assigned_to / assignment_group
    # -----------------------------------------------------------------------
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
                if _counters is not None and (
                    _is_entity_ambiguous(org_id, "person", assigned_name, entities)
                    or _is_entity_ambiguous(org_id, "team", group_name, entities)
                ):
                    _counters["skipped_ambiguous"] = _counters.get("skipped_ambiguous", 0) + 1
                continue
            if write_once(
                person,
                team,
                "member_of",
                {"field": "assignment_group", "source": "servicenow"},
            ):
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
                if _counters is not None and (
                    _is_entity_ambiguous(org_id, "object", inc_number, entities)
                    or _is_entity_ambiguous(org_id, "person", esc_name, entities)
                    or _is_entity_ambiguous(org_id, "team", esc_name, entities)
                ):
                    _counters["skipped_ambiguous"] = _counters.get("skipped_ambiguous", 0) + 1
                continue
            if write_once(
                from_ent,
                to_ent,
                "escalates_to",
                {"field": "escalated_to", "source": "servicenow"},
            ):
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
                if _counters is not None and (
                    _is_entity_ambiguous(org_id, "object", issue_key, entities)
                    or _is_entity_ambiguous(org_id, "person", esc_name, entities)
                    or _is_entity_ambiguous(org_id, "team", esc_name, entities)
                ):
                    _counters["skipped_ambiguous"] = _counters.get("skipped_ambiguous", 0) + 1
                continue
            if write_once(
                from_ent,
                to_ent,
                "escalates_to",
                {"field": "escalation_label", "source": "jira"},
            ):
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


# ---------------------------------------------------------------------------
# map_inferred_from_detectors() — T4 secondary deliverable
# ---------------------------------------------------------------------------
#
# Inferred relationships are HYPOTHESES, not graph truth. Co-firing of two
# detectors in the same run is correlation, never proven causation. These
# edges are written to the database in EVERY run where both detectors fire,
# regardless of the INFERRED_RELATIONSHIPS_ENABLED flag (the flag is a
# *surfacing* control implemented in T5 — it gates whether these edges appear
# in OppEnrichment.relationships and the evidence trace, NOT whether they are
# stored). Suppressing storage on the flag would silently starve T3-S16-A
# Stage 3 causal analysis, which reads the full stored history of inferred
# edges to validate whether co-firing reflects a real structural dependency.
#
# All edges created here have confidence=0.6 and inferred=True. These are
# constants of an inferred edge — never parameterised, never derived.
#
# Every inferred edge carries an evidence dict with:
#   rationale     — describes the co-firing pattern that produced the edge.
#   detector_ids  — the detectors whose co-firing produced this edge.
#   note          — the Stage 3 validation warning (verbatim, load-bearing).

# Verbatim validation note attached to every inferred edge. T3-S16-A and the
# evidence trace read this exact text — do not reword it.
INFERRED_VALIDATION_NOTE = (
    "Validate with Stage 3 causal analysis before treating as truth"
)

# nCino co-firing rules that produce Process -> Process depends_on edges.
# Each rule: (detector_a, detector_b) must BOTH fire, then a depends_on edge is
# drawn from the `from_detector` process to the `to_detector` process.
# The edge direction encodes "downstream process depends_on upstream process".
_NCINO_DEPENDS_ON_RULES: tuple[tuple[frozenset[str], str, str], ...] = (
    # Rule 1: Covenant Review depends_on Loan Origination.
    (
        frozenset({LOAN_ORIGINATION_DETECTOR_ID, COVENANT_TRACKING_DETECTOR_ID}),
        COVENANT_TRACKING_DETECTOR_ID,
        LOAN_ORIGINATION_DETECTOR_ID,
    ),
    # Rule 2: Document Collection depends_on Loan Origination.
    (
        frozenset({LOAN_ORIGINATION_DETECTOR_ID, CHECKLIST_BOTTLENECK_DETECTOR_ID}),
        CHECKLIST_BOTTLENECK_DETECTOR_ID,
        LOAN_ORIGINATION_DETECTOR_ID,
    ),
    # Rule 3: Disbursement depends_on Covenant Review.
    (
        frozenset({DISBURSEMENT_OVERDUE_DETECTOR_ID, COVENANT_TRACKING_DETECTOR_ID}),
        DISBURSEMENT_OVERDUE_DETECTOR_ID,
        COVENANT_TRACKING_DETECTOR_ID,
    ),
)

# Rule 4 (System -> System routes_to): when COVENANT_TRACKING_GAP and
# DB_SLA_BREACH_RATE co-fire, the SQL Server ITSM system routes_to Salesforce.
# Systems are resolved by signal_source; the alias lists below are the fallback
# canonical names when a detector's signal_source is absent from the run.
# nCino Rule 4: SQL Server routes_to Salesforce.
_ROUTES_TO_RULE_DETECTORS = frozenset(
    {COVENANT_TRACKING_DETECTOR_ID, DB_SLA_BREACH_RATE_DETECTOR_ID}
)

# ---------------------------------------------------------------------------
# Service Cloud co-firing rules (SC-1 … SC-4)
# ---------------------------------------------------------------------------
# SC-1: Case Routing Process depends_on Case Automation Process
#        when REPETITIVE_AUTOMATION + HANDOFF_FRICTION co-fire. The handoff
#        overhead is downstream of the repetitive automation gap.
# SC-2: Approval Routing Process depends_on Case Routing Process
#        when APPROVAL_BOTTLENECK + HANDOFF_FRICTION co-fire. Approval waits
#        are amplified by the case-routing friction already present.
# SC-3: Knowledge Management Process depends_on Case Automation Process
#        when KNOWLEDGE_GAP + REPETITIVE_AUTOMATION co-fire. Agents escalate
#        manually (automation gap) because knowledge is missing.
_SC_DEPENDS_ON_RULES: tuple[tuple[frozenset[str], str, str], ...] = (
    # SC-1
    (
        frozenset({REPETITIVE_AUTOMATION_DETECTOR_ID, HANDOFF_FRICTION_DETECTOR_ID}),
        HANDOFF_FRICTION_DETECTOR_ID,
        REPETITIVE_AUTOMATION_DETECTOR_ID,
    ),
    # SC-2
    (
        frozenset({APPROVAL_BOTTLENECK_DETECTOR_ID, HANDOFF_FRICTION_DETECTOR_ID}),
        APPROVAL_BOTTLENECK_DETECTOR_ID,
        HANDOFF_FRICTION_DETECTOR_ID,
    ),
    # SC-3
    (
        frozenset({KNOWLEDGE_GAP_DETECTOR_ID, REPETITIVE_AUTOMATION_DETECTOR_ID}),
        KNOWLEDGE_GAP_DETECTOR_ID,
        REPETITIVE_AUTOMATION_DETECTOR_ID,
    ),
)

# SC-4: ServiceNow routes_to Salesforce when CROSS_SYSTEM_ECHO +
#        INTEGRATION_CONCENTRATION co-fire (tickets echo across the boundary
#        and the integration is over-concentrated on a single flow).
_SC_ROUTES_TO_RULE_DETECTORS = frozenset(
    {CROSS_SYSTEM_ECHO_DETECTOR_ID, INTEGRATION_CONCENTRATION_DETECTOR_ID}
)
_SERVICENOW_SYSTEM_ALIASES = (
    "servicenow", "service now", "service_now", "snow",
)

INFERRED_RULE_DETECTOR_IDS = (
    frozenset(
        detector_id
        for required, _, _ in _NCINO_DEPENDS_ON_RULES
        for detector_id in required
    )
    | _ROUTES_TO_RULE_DETECTORS
    | frozenset(
        detector_id
        for required, _, _ in _SC_DEPENDS_ON_RULES
        for detector_id in required
    )
    | _SC_ROUTES_TO_RULE_DETECTORS
)
_SQLSERVER_SYSTEM_ALIASES = (
    "sqlserver", "sql server", "sql_server", "mssql", "microsoft sql server",
)
_SALESFORCE_SYSTEM_ALIASES = ("salesforce", "sfdc")


def get_process_entity(
    org_id: str,
    detector_id: str,
    entities: List[Entity],
) -> Optional[Entity]:
    """Resolve the Process entity that represents a detector.

    extract_entities() creates one Process entity per distinct detector_id,
    using the detector_id as the display_name (canonical_name is therefore the
    canonicalised detector_id). This helper maps a detector_id back to that
    Process entity so an inferred edge can use it as an endpoint.

    Returns None when no Process entity for the detector is present in the
    run's entity list — the edge is then skipped (an inferred edge with a
    missing endpoint is meaningless).

    Process entities use stable AgentIQ identifiers and are never ambiguous,
    so — unlike get_resolved_entity() — resolution_status is not gated here.
    """
    if not detector_id or not detector_id.strip():
        return None
    canonical = _canonicalize(detector_id)
    for entity in entities:
        if (
            entity.org_id == org_id
            and entity.entity_type == "process"
            and entity.canonical_name == canonical
        ):
            return entity
    return None


def get_system_entity(
    org_id: str,
    candidate_names: List[str],
    entities: List[Entity],
) -> Optional[Entity]:
    """Resolve a System entity by trying candidate names in priority order.

    extract_entities() creates one System entity per distinct signal_source,
    using the signal_source as the display_name. A detector's signal_source
    (e.g. 'sqlserver', 'salesforce') is the primary candidate; alias lists
    provide fallbacks when connector_id naming varies.

    Returns the first System entity whose canonical_name matches a candidate,
    or None when none match.
    """
    canon_candidates = [
        _canonicalize(name) for name in candidate_names if name and str(name).strip()
    ]
    if not canon_candidates:
        return None
    for cand in canon_candidates:
        for entity in entities:
            if (
                entity.org_id == org_id
                and entity.entity_type == "system"
                and entity.canonical_name == cand
            ):
                return entity
    return None


def _inferred_evidence(detector_ids: List[str]) -> Dict[str, Any]:
    """Build the evidence dict carried by every inferred edge.

    detector_ids is sorted for deterministic output. The note is verbatim and
    load-bearing — T3-S16-A causal analysis and the evidence trace read it to
    understand that the edge is an unvalidated hypothesis.
    """
    ordered = sorted(detector_ids)
    pattern = " + ".join(ordered)
    return {
        "rationale": (
            f"Co-firing pattern: {pattern} fired in the same run — "
            "correlation, not confirmed as structural dependency"
        ),
        "detector_ids": ordered,
        "note": INFERRED_VALIDATION_NOTE,
    }


def map_inferred_from_detectors(
    org_id: str,
    run_id: str,
    detector_results: List[Any],
    entities: List[Entity],
) -> int:
    """Write inferred depends_on / routes_to edges from detector co-firing.

    Edges are persisted to the database in EVERY run where both detectors of a
    rule fire, REGARDLESS of the INFERRED_RELATIONSHIPS_ENABLED flag. The flag
    controls surfacing (T5), not storage. All edges have confidence=0.6 and
    inferred=True — set here as constants, never parameterised.

    Eight co-firing rules — nCino (1-4) and Service Cloud (SC-1 to SC-4):
      1. LOAN_ORIGINATION_ROUTING_FRICTION + COVENANT_TRACKING_GAP
         -> Covenant Review depends_on Loan Origination
      2. LOAN_ORIGINATION_ROUTING_FRICTION + CHECKLIST_BOTTLENECK
         -> Document Collection depends_on Loan Origination
      3. DISBURSEMENT_OVERDUE + COVENANT_TRACKING_GAP
         -> Disbursement depends_on Covenant Review
      4. COVENANT_TRACKING_GAP + DB_SLA_BREACH_RATE
         -> SQL Server ITSM routes_to Salesforce
      SC-1. REPETITIVE_AUTOMATION + HANDOFF_FRICTION
         -> Case Routing depends_on Case Automation
      SC-2. APPROVAL_BOTTLENECK + HANDOFF_FRICTION
         -> Approval Routing depends_on Case Routing
      SC-3. KNOWLEDGE_GAP + REPETITIVE_AUTOMATION
         -> Knowledge Management depends_on Case Automation
      SC-4. CROSS_SYSTEM_ECHO + INTEGRATION_CONCENTRATION
         -> ServiceNow routes_to Salesforce

    A rule only writes an edge when both its detectors fired AND both edge
    endpoints resolve to entities in the run's entity list. A rule whose
    endpoints are missing is skipped; individual rule failures are caught and
    logged and never raise to the caller (relationship mapping is non-blocking
    per AC9).

    Args:
        org_id:           Workspace identifier. All lookups scoped to this.
        run_id:           Current discovery run ID.
        detector_results: DetectorResult objects from the detector phase. Each
                          exposes .detector_id and .signal_source.
        entities:         Entity list from extract_entities() for this run.

    Returns:
        Number of upsert_relationship() calls made (created or updated edges).
    """
    count = 0

    fired = {
        did
        for did in (
            _safe_detector_id(dr) for dr in (detector_results or [])
        )
        if did
    }
    source_by_detector = {
        did: src
        for did, src in (
            (_safe_detector_id(dr), _safe_signal_source(dr))
            for dr in (detector_results or [])
        )
        if did
    }

    # Rules 1-3: Process -> Process depends_on edges.
    for required, from_detector, to_detector in _NCINO_DEPENDS_ON_RULES:
        if not required.issubset(fired):
            continue
        try:
            from_proc = get_process_entity(org_id, from_detector, entities)
            to_proc = get_process_entity(org_id, to_detector, entities)
            if from_proc is None or to_proc is None:
                logger.debug(
                    "map_inferred_from_detectors — depends_on rule %s skipped: "
                    "missing process entity (from=%s to=%s)",
                    sorted(required), from_proc is not None, to_proc is not None,
                )
                continue
            upsert_relationship(
                org_id=org_id,
                from_entity_id=str(from_proc.id),
                to_entity_id=str(to_proc.id),
                relationship_type="depends_on",
                confidence=INFERRED_CONFIDENCE,
                inferred=True,
                run_id=run_id,
                evidence=_inferred_evidence([from_detector, to_detector]),
            )
            count += 1
        except Exception as exc:
            logger.debug(
                "map_inferred_from_detectors — depends_on rule %s failed: %s",
                sorted(required), exc,
            )

    # Rule 4: System -> System routes_to edge (SQL Server -> Salesforce).
    if _ROUTES_TO_RULE_DETECTORS.issubset(fired):
        try:
            from_source = source_by_detector.get(DB_SLA_BREACH_RATE_DETECTOR_ID)
            to_source = source_by_detector.get(COVENANT_TRACKING_DETECTOR_ID)
            from_system = get_system_entity(
                org_id,
                [from_source, *_SQLSERVER_SYSTEM_ALIASES],
                entities,
            )
            to_system = get_system_entity(
                org_id,
                [to_source, *_SALESFORCE_SYSTEM_ALIASES],
                entities,
            )
            if from_system is None or to_system is None:
                logger.debug(
                    "map_inferred_from_detectors — routes_to rule skipped: "
                    "missing system entity (from=%s to=%s)",
                    from_system is not None, to_system is not None,
                )
            else:
                upsert_relationship(
                    org_id=org_id,
                    from_entity_id=str(from_system.id),
                    to_entity_id=str(to_system.id),
                    relationship_type="routes_to",
                    confidence=INFERRED_CONFIDENCE,
                    inferred=True,
                    run_id=run_id,
                    evidence=_inferred_evidence(
                        [COVENANT_TRACKING_DETECTOR_ID, DB_SLA_BREACH_RATE_DETECTOR_ID]
                    ),
                )
                count += 1
        except Exception as exc:
            logger.debug("map_inferred_from_detectors — routes_to rule failed: %s", exc)

    # SC Rules 1-3: Service Cloud Process -> Process depends_on edges.
    for required, from_detector, to_detector in _SC_DEPENDS_ON_RULES:
        if not required.issubset(fired):
            continue
        try:
            from_proc = get_process_entity(org_id, from_detector, entities)
            to_proc = get_process_entity(org_id, to_detector, entities)
            if from_proc is None or to_proc is None:
                logger.debug(
                    "map_inferred_from_detectors — SC depends_on rule %s skipped: "
                    "missing process entity (from=%s to=%s)",
                    sorted(required), from_proc is not None, to_proc is not None,
                )
                continue
            upsert_relationship(
                org_id=org_id,
                from_entity_id=str(from_proc.id),
                to_entity_id=str(to_proc.id),
                relationship_type="depends_on",
                confidence=INFERRED_CONFIDENCE,
                inferred=True,
                run_id=run_id,
                evidence=_inferred_evidence([from_detector, to_detector]),
            )
            count += 1
        except Exception as exc:
            logger.debug(
                "map_inferred_from_detectors — SC depends_on rule %s failed: %s",
                sorted(required), exc,
            )

    # SC Rule 4: System -> System routes_to edge (ServiceNow -> Salesforce).
    if _SC_ROUTES_TO_RULE_DETECTORS.issubset(fired):
        try:
            from_source = source_by_detector.get(CROSS_SYSTEM_ECHO_DETECTOR_ID)
            to_source = source_by_detector.get(INTEGRATION_CONCENTRATION_DETECTOR_ID)
            from_system = get_system_entity(
                org_id,
                [from_source, *_SERVICENOW_SYSTEM_ALIASES],
                entities,
            )
            to_system = get_system_entity(
                org_id,
                [to_source, *_SALESFORCE_SYSTEM_ALIASES],
                entities,
            )
            if from_system is None or to_system is None:
                logger.debug(
                    "map_inferred_from_detectors — SC routes_to rule skipped: "
                    "missing system entity (from=%s to=%s)",
                    from_system is not None, to_system is not None,
                )
            else:
                upsert_relationship(
                    org_id=org_id,
                    from_entity_id=str(from_system.id),
                    to_entity_id=str(to_system.id),
                    relationship_type="routes_to",
                    confidence=INFERRED_CONFIDENCE,
                    inferred=True,
                    run_id=run_id,
                    evidence=_inferred_evidence(
                        [CROSS_SYSTEM_ECHO_DETECTOR_ID, INTEGRATION_CONCENTRATION_DETECTOR_ID]
                    ),
                )
                count += 1
        except Exception as exc:
            logger.debug("map_inferred_from_detectors — SC routes_to rule failed: %s", exc)

    logger.info(
        "map_inferred_from_detectors — run=%s org=%s inferred_edges_written=%d",
        run_id, org_id, count,
    )
    return count


def _safe_detector_id(dr: Any) -> Optional[str]:
    """Read .detector_id from a DetectorResult-like object, tolerating dicts."""
    if isinstance(dr, dict):
        val = dr.get("detector_id")
    else:
        val = getattr(dr, "detector_id", None)
    if val is None:
        return None
    s = str(val).strip()
    return s or None


def _safe_signal_source(dr: Any) -> Optional[str]:
    """Read .signal_source from a DetectorResult-like object, tolerating dicts."""
    if isinstance(dr, dict):
        val = dr.get("signal_source")
    else:
        val = getattr(dr, "signal_source", None)
    if val is None:
        return None
    s = str(val).strip()
    return s or None


def _record_mapping_completed(
    *,
    org_id: str,
    run_id: str,
    observed: int,
    inferred: int,
    skipped_ambiguous: int,
    duration_ms: float,
) -> None:
    """Emit relationship.mapping_completed once without letting telemetry fail mapping."""
    try:
        from app.telemetry import record_event
        record_event(
            "relationship.mapping_completed",
            {
                "org_id": org_id,
                "run_id": run_id,
                "observed_count": observed,
                "inferred_count": inferred,
                "skipped_ambiguous_count": skipped_ambiguous,
                "mapping_duration_ms": round(duration_ms, 2),
            },
        )
    except Exception as exc:
        logger.warning(
            "relationship.mapping_completed telemetry failed: run_id=%s org_id=%s error=%s",
            run_id, org_id, exc,
        )


# ---------------------------------------------------------------------------
# map_relationships() — T6 runner orchestrator
# ---------------------------------------------------------------------------

def map_relationships(
    org_id: str,
    run_id: str,
    ingestor_data: Dict[str, Any],
    detector_results: List[Any],
    entities: List[Entity],
) -> Dict[str, int]:
    """Single relationship-mapping entry point called by the discovery runner.

    This is the ONLY relationship function runner.py invokes. It runs the two
    mapping passes in sequence:
      1. map_directly_observed()       — observed edges (graph truth).
      2. map_inferred_from_detectors() — inferred co-firing edges (hypotheses).

    Must be called AFTER extract_entities() completes: both passes draw edges
    only between resolved entity rows written during extraction. Calling it
    before extraction would find no entities and produce an empty graph.

    On success it emits the relationship.mapping_completed telemetry event
    (T9) exactly once. The event is emitted only when both passes complete —
    if this function raises, the runner's non-blocking wrapper swallows it and
    no event is emitted; the absence of the event alongside the runner's
    warning log is the diagnostic signal for a failed mapping run.

    Note: this function may raise (e.g. on DB errors). It is the RUNNER's
    responsibility to wrap the call in try/except so relationship mapping is
    never on the critical path for opportunity delivery (AC9).

    Args:
        org_id:           Workspace identifier. All edges scoped to this.
        run_id:           Current discovery run ID.
        ingestor_data:    Dict keyed by connector name (salesforce/servicenow/jira).
        detector_results: DetectorResult objects from the detector phase.
        entities:         Resolved entity list returned by extract_entities().

    Returns:
        Counts: {"observed": int, "inferred": int, "total": int}.
    """
    # Guarantee the table exists even if migrations have not been applied in
    # this environment — both passes upsert into it. Idempotent.
    ensure_entity_relationships_table()

    t0 = time.monotonic()
    if not entities:
        duration_ms = (time.monotonic() - t0) * 1000.0
        logger.warning("map_relationships skipped: no entities from extraction")
        _record_mapping_completed(
            org_id=org_id,
            run_id=run_id,
            observed=0,
            inferred=0,
            skipped_ambiguous=0,
            duration_ms=duration_ms,
        )
        logger.info(
            "map_relationships — run=%s org=%s observed=0 inferred=0 "
            "skipped_ambiguous=0 duration_ms=%.1f",
            run_id, org_id, duration_ms,
        )
        return {"observed": 0, "inferred": 0, "total": 0}

    counters: Dict[str, int] = {"skipped_ambiguous": 0}
    observed = map_directly_observed(org_id, run_id, ingestor_data, entities or [], _counters=counters)
    inferred = map_inferred_from_detectors(org_id, run_id, detector_results or [], entities or [])
    duration_ms = (time.monotonic() - t0) * 1000.0
    skipped_ambiguous = counters.get("skipped_ambiguous", 0)

    # T9 telemetry — success path only, exactly once per run. Guarded so a
    # telemetry failure never turns a successful mapping run into a failed one.
    _record_mapping_completed(
        org_id=org_id,
        run_id=run_id,
        observed=observed,
        inferred=inferred,
        skipped_ambiguous=skipped_ambiguous,
        duration_ms=duration_ms,
    )

    logger.info(
        "map_relationships — run=%s org=%s observed=%d inferred=%d skipped_ambiguous=%d duration_ms=%.1f",
        run_id, org_id, observed, inferred, skipped_ambiguous, duration_ms,
    )
    return {"observed": observed, "inferred": inferred, "total": observed + inferred}
