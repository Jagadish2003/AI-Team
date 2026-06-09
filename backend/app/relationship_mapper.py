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
)

try:
    from discovery.detectors.checklist_bottleneck import DETECTOR_ID as CHECKLIST_BOTTLENECK_DETECTOR_ID
    from discovery.detectors.covenant_tracking_gap import DETECTOR_ID as COVENANT_TRACKING_DETECTOR_ID
    from discovery.detectors.db_sla_breach_rate import DETECTOR_ID as DB_SLA_BREACH_RATE_DETECTOR_ID
    from discovery.detectors.disbursement_overdue import DETECTOR_ID as DISBURSEMENT_OVERDUE_DETECTOR_ID
    from discovery.detectors.loan_origination_routing_friction import DETECTOR_ID as LOAN_ORIGINATION_DETECTOR_ID
except ModuleNotFoundError:  # project-root execution uses backend as package
    from backend.discovery.detectors.checklist_bottleneck import DETECTOR_ID as CHECKLIST_BOTTLENECK_DETECTOR_ID
    from backend.discovery.detectors.covenant_tracking_gap import DETECTOR_ID as COVENANT_TRACKING_DETECTOR_ID
    from backend.discovery.detectors.db_sla_breach_rate import DETECTOR_ID as DB_SLA_BREACH_RATE_DETECTOR_ID
    from backend.discovery.detectors.disbursement_overdue import DETECTOR_ID as DISBURSEMENT_OVERDUE_DETECTOR_ID
    from backend.discovery.detectors.loan_origination_routing_friction import DETECTOR_ID as LOAN_ORIGINATION_DETECTOR_ID

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
    # PII guard: mapper-owned evidence stores field/source names, detector IDs,
    # and rationale only. Do not pass raw display names, case titles, amounts,
    # or external record values into this generic persistence helper.
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
    canonical = _canonicalize(display_name)
    for entity in entities:
        if (
            entity.org_id == org_id
            and entity.entity_type == entity_type
            and entity.canonical_name == canonical
        ):
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
                    if _counters is not None and (
                        _is_entity_ambiguous(org_id, "person", owner_name, entities)
                        or _is_entity_ambiguous(org_id, "object", obj_name, entities)
                    ):
                        _counters["skipped_ambiguous"] = _counters.get("skipped_ambiguous", 0) + 1
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
                    if _counters is not None and (
                        _is_entity_ambiguous(org_id, "person", owner_name, entities)
                        or _is_entity_ambiguous(org_id, "object", obj_name, entities)
                    ):
                        _counters["skipped_ambiguous"] = _counters.get("skipped_ambiguous", 0) + 1
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
                if _counters is not None and (
                    _is_entity_ambiguous(org_id, "person", assigned_name, entities)
                    or _is_entity_ambiguous(org_id, "team", group_name, entities)
                ):
                    _counters["skipped_ambiguous"] = _counters.get("skipped_ambiguous", 0) + 1
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
                if _counters is not None and (
                    _is_entity_ambiguous(org_id, "object", inc_number, entities)
                    or _is_entity_ambiguous(org_id, "person", esc_name, entities)
                    or _is_entity_ambiguous(org_id, "team", esc_name, entities)
                ):
                    _counters["skipped_ambiguous"] = _counters.get("skipped_ambiguous", 0) + 1
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
                if _counters is not None and (
                    _is_entity_ambiguous(org_id, "object", issue_key, entities)
                    or _is_entity_ambiguous(org_id, "person", esc_name, entities)
                    or _is_entity_ambiguous(org_id, "team", esc_name, entities)
                ):
                    _counters["skipped_ambiguous"] = _counters.get("skipped_ambiguous", 0) + 1
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
_ROUTES_TO_RULE_DETECTORS = frozenset(
    {COVENANT_TRACKING_DETECTOR_ID, DB_SLA_BREACH_RATE_DETECTOR_ID}
)
INFERRED_RULE_DETECTOR_IDS = frozenset(
    detector_id
    for required, _, _ in _NCINO_DEPENDS_ON_RULES
    for detector_id in required
) | _ROUTES_TO_RULE_DETECTORS
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

    Four co-firing rules (Section 6):
      1. LOAN_ORIGINATION_ROUTING_FRICTION + COVENANT_TRACKING_GAP
         -> Covenant Review depends_on Loan Origination
      2. LOAN_ORIGINATION_ROUTING_FRICTION + CHECKLIST_BOTTLENECK
         -> Document Collection depends_on Loan Origination
      3. DISBURSEMENT_OVERDUE + COVENANT_TRACKING_GAP
         -> Disbursement depends_on Covenant Review
      4. COVENANT_TRACKING_GAP + DB_SLA_BREACH_RATE
         -> SQL Server ITSM routes_to Salesforce

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
