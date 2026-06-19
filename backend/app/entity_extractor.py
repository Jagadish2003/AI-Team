"""Entity extraction from ingestor runs — T3-S12-A T3.

extract_entities() is the orchestration layer. It reads raw ingestor output
and detector results, then calls resolve_or_create_entity() for every entity
found. It must never raise — failures are logged and the run continues.

Extraction sources per Section 4b:
  Salesforce/nCino: Person (OwnerId/approver_ids), Object (record IDs),
                    Process (detector_id)
  Jira:             Person (assignee, reporter), Team (project), Object (issue.key)
  ServiceNow:       Person (assigned_to, caller_id), Team (assignment_group),
                    Object (incident number)
  All sources:      System (signal_source), Process (detector_id)

Confidence rules (Section 4b):
  source_record_id present → 1.0 (unique system ID)
  Name-based (Jira/SN)    → 0.8
  System/Process entities  → 1.0 (stable AgentIQ identifiers)

Service account filtering (Section 8): entities with run_count < 3 are
filtered from the OppEnrichment evidence trace but retained in the DB.
"""
from __future__ import annotations

import json
import logging
import os
import re
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.entity_resolution import resolve_or_create_entity as _core_resolve_or_create_entity
from database.models.entities import Entity, ENTITY_MIN_RUN_COUNT

logger = logging.getLogger(__name__)

# Entities seen in fewer runs than this are treated as service accounts and
# filtered from the OppEnrichment evidence trace. Sourced from a single shared
# constant so the threshold can never drift from the test suite that asserts it.
_MIN_DISPLAY_RUN_COUNT = ENTITY_MIN_RUN_COUNT


# ===========================================================================
# ENT-1 — Overlay awareness (Tasks T4 + T5)
#
# The overlay layer is ADDITIVE. When no overlay is registered for an
# (org_id, connector_id), _overlay_context is None and resolve_or_create_entity
# below is a transparent pass-through to the core resolver — so default
# T3-S12-A extraction behaves byte-for-byte as before (AC2).
#
# When an overlay IS active, the same wrapper:
#   - filters service-account display_names (T5 / AC5) — the entity is skipped
#     and counted, never stored;
#   - records cross-system provenance in metadata.sources (AC4) so a Person
#     anchored to a Salesforce OwnerId and later seen by name in ServiceNow /
#     Jira lists all three systems while retaining confidence 1.0.
#
# All per-source extractors call resolve_or_create_entity (this wrapper) by
# name, so overlay behavior is applied without touching any of them.
# ===========================================================================


class _FilteredServiceAccount(Exception):
    """Raised internally when a display_name matches the active overlay's
    service-account patterns.

    Kept only as an internal marker type for older tests/imports. The resolver
    wrapper now handles filtering itself and returns None, so broad
    ``except Exception`` blocks in per-source extractors never mistake filtered
    service accounts for extraction failures.
    """


class _OverlayContext:
    """Per-extraction overlay state: the compiled service-account filter and the
    running set of filtered identities. Held in a ContextVar so concurrent runs
    (async/threaded) never share state."""

    def __init__(self, service_account_patterns: Optional[List[str]] = None) -> None:
        self._compiled: List[re.Pattern] = []
        for pattern in service_account_patterns or []:
            try:
                self._compiled.append(re.compile(pattern, re.IGNORECASE))
            except re.error as exc:
                logger.warning(
                    "entity overlay: invalid service_account pattern %r skipped: %s",
                    pattern,
                    exc,
                )
        # Distinct filtered identities (canonicalised) — len() is the count.
        self.filtered_names: set[str] = set()

    def matches_service_account(self, display_name: Optional[str]) -> bool:
        if not display_name or not self._compiled:
            return False
        return any(rx.search(display_name) for rx in self._compiled)

    def record_filtered(self, display_name: Optional[str]) -> None:
        self.filtered_names.add(" ".join((display_name or "").split()).lower())

    @property
    def filtered_count(self) -> int:
        return len(self.filtered_names)


def _append_entity(entities: List[Entity], entity: Optional[Entity]) -> None:
    """Append only real entities; service-account filtering returns None."""
    if entity is not None:
        entities.append(entity)


def _deduplicate_entities(entities: List[Entity]) -> List[Entity]:
    """Return one current Entity object per canonical database row."""
    by_id: Dict[str, Entity] = {}
    for entity in entities:
        # Reassignment preserves first-seen order and retains the freshest state.
        by_id[str(entity.id)] = entity
    return list(by_id.values())


# Active overlay context for the current extraction, or None when no overlay
# applies. ContextVar keeps this safe across concurrent extractions.
_overlay_context: ContextVar[Optional[_OverlayContext]] = ContextVar(
    "entity_overlay_context", default=None
)


def resolve_or_create_entity(**kwargs: Any) -> Optional[Entity]:
    """Overlay-aware shim around entity_resolution.resolve_or_create_entity.

    Defined with the same name the per-source extractors already call, so the
    overlay behavior is injected without modifying any extractor body.

    No overlay active (default path): a transparent pass-through — identical to
    the core resolver (AC2).

    Overlay active:
      - if display_name matches a service-account pattern, the identity is
        recorded and None is returned before any DB write (T5 / AC5);
      - otherwise the entity is resolved/created, and its source_system is added
        to metadata.sources for cross-system provenance (AC4).
    """
    ctx = _overlay_context.get()
    if ctx is not None and ctx.matches_service_account(kwargs.get("display_name")):
        ctx.record_filtered(kwargs.get("display_name"))
        metadata = kwargs.get("metadata")
        path = "overlay" if isinstance(metadata, dict) and metadata.get("overlay_version") else "default"
        logger.debug(
            "entity overlay: filtered service-account identity from %s path "
            "(source_system=%s display_name=%r)",
            path,
            kwargs.get("source_system"),
            kwargs.get("display_name"),
        )
        return None

    entity = _core_resolve_or_create_entity(**kwargs)

    if ctx is not None and entity is not None:
        _record_entity_source(entity, kwargs.get("source_system"))
    return entity


def _record_entity_source(entity: Entity, source_system: Optional[str]) -> None:
    """Additively merge *source_system* into the entity's metadata.sources.

    Cross-system provenance tracking (AC4). Only ever invoked while an overlay
    is active, so the default (no-overlay) path never touches metadata. Updates
    only the metadata JSON column — the locked entity schema is unchanged.
    TODO(T3-S15-A): promote metadata.sources to a queryable/indexed column before
    graph queries need to ask for entities seen in multiple systems.
    Best-effort: a failure here never breaks extraction.
    """
    if not source_system:
        return
    try:
        from app import db

        conn = db.connect()
        try:
            # Serialize the metadata read/merge/write so concurrent runs do not
            # lose a source_system addition for the same entity row. Lock the row
            # FOR UPDATE within the implicit transaction psycopg2 opens.
            cur = conn.cursor()
            cur.execute(
                "SELECT metadata FROM entities WHERE id = %s FOR UPDATE", (str(entity.id),)
            )
            row = cur.fetchone()
            if row is None:
                conn.commit()
                return
            raw = row[0]
            meta = json.loads(raw) if isinstance(raw, str) and raw else (raw or {})
            if not isinstance(meta, dict):
                meta = {}
            sources = meta.get("sources")
            if not isinstance(sources, list):
                sources = []
            if source_system not in sources:
                sources.append(source_system)
            meta["sources"] = sources
            cur.execute(
                "UPDATE entities SET metadata = %s, updated_at = %s WHERE id = %s",
                (json.dumps(meta), datetime.now(timezone.utc).isoformat(), str(entity.id)),
            )
            conn.commit()
            # Reflect the merged metadata on the returned in-memory object too.
            entity.metadata = meta
        finally:
            conn.close()
    except Exception as exc:  # pragma: no cover — provenance is best-effort
        logger.debug("entity source provenance update skipped: %s", exc)


def _safe_str(val: Any) -> Optional[str]:
    """Return a non-empty stripped string or None."""
    if not val:
        return None
    s = str(val).strip()
    return s if s else None


def _iter_records(value: Any) -> List[Dict[str, Any]]:
    """Normalize source payload fragments to a list of record dictionaries."""
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        return [value]
    return []


def _first_record_str(record: Dict[str, Any], keys: tuple[str, ...]) -> Optional[str]:
    """Return the first non-empty string from common source-field variants."""
    for key in keys:
        val = record.get(key)
        if isinstance(val, dict):
            nested = _first_record_str(
                val,
                (
                    "display_value",
                    "displayValue",
                    "displayName",
                    "name",
                    "Name",
                    "value",
                    "id",
                    "Id",
                    "key",
                ),
            )
            if nested:
                return nested
            continue
        text = _safe_str(val)
        if text:
            return text
    return None


def _ref_name_and_id(value: Any) -> tuple[Optional[str], Optional[str]]:
    """Read a source reference that may be a string or {display,value,id} dict."""
    if isinstance(value, list):
        return _ref_name_and_id(value[0]) if value else (None, None)
    if isinstance(value, dict):
        name = _first_record_str(
            value,
            (
                "display_value",
                "displayValue",
                "displayName",
                "name",
                "Name",
                "label",
                "value",
                "id",
                "Id",
                "key",
            ),
        )
        source_id = _first_record_str(value, ("id", "Id", "value", "sys_id", "key"))
        return name, source_id
    text = _safe_str(value)
    return text, text


# ---------------------------------------------------------------------------
# Per-source extractors
# ---------------------------------------------------------------------------

def _extract_salesforce_entities(
    *,
    org_id: str,
    run_id: str,
    sf_data: Dict[str, Any],
) -> List[Entity]:
    """Extract Person, Team, Project, and Object entities from Salesforce/nCino output."""
    entities: List[Entity] = []

    # Person: approver_ids inside approval_processes act as OwnerId equivalents.
    # source_record_id is present → confidence=1.0.
    for proc in sf_data.get("approval_processes", []) or []:
        for approver_id in proc.get("approver_ids", []) or []:
            name = _safe_str(approver_id)
            if not name:
                continue
            try:
                e = resolve_or_create_entity(
                    org_id=org_id,
                    entity_type="person",
                    display_name=name,
                    source_system="salesforce",
                    source_record_id=name,
                    run_id=run_id,
                    metadata={"process_name": proc.get("process_name")},
                )
                _append_entity(entities, e)
            except Exception as exc:
                logger.warning("SF person extraction failed for %s: %s", name, exc)

    # Person: OwnerId / AssignedTo fields from common Salesforce record buckets.
    # These are source IDs, so they carry confidence=1.0 through resolution.
    for collection_name in (
        "records",
        "cases",
        "tasks",
        "opportunities",
        "objects",
        "sample_records",
    ):
        for record in _iter_records(sf_data.get(collection_name)):
            for field_name in ("OwnerId", "owner_id", "AssignedTo", "assigned_to"):
                display_name, source_id = _ref_name_and_id(record.get(field_name))
                if not display_name:
                    continue
                try:
                    e = resolve_or_create_entity(
                        org_id=org_id,
                        entity_type="person",
                        display_name=display_name,
                        source_system="salesforce",
                        source_record_id=source_id or display_name,
                        run_id=run_id,
                        metadata={"source": collection_name, "field": field_name},
                    )
                    _append_entity(entities, e)
                except Exception as exc:
                    logger.warning(
                        "SF %s extraction failed for %s: %s",
                        field_name,
                        display_name,
                        exc,
                    )

    # Object: records from common Salesforce buckets. Sprint 13 relationship
    # mapping needs a resolved Object endpoint for OwnerId -> record edges.
    for collection_name in (
        "records",
        "cases",
        "tasks",
        "opportunities",
        "objects",
        "sample_records",
        "loans",
        "loan_applications",
    ):
        for record in _iter_records(sf_data.get(collection_name)):
            record_id = _first_record_str(
                record,
                (
                    "Id",
                    "id",
                    "record_id",
                    "CaseId",
                    "case_id",
                    "TaskId",
                    "task_id",
                    "OpportunityId",
                    "opportunity_id",
                    "loan_id",
                    "LoanId",
                ),
            )
            display_name = _first_record_str(
                record,
                (
                    "Name",
                    "name",
                    "CaseNumber",
                    "case_number",
                    "number",
                    "record_name",
                    "record_id",
                    "Id",
                    "id",
                ),
            )
            if not display_name:
                continue
            try:
                attrs = record.get("attributes") if isinstance(record.get("attributes"), dict) else {}
                e = resolve_or_create_entity(
                    org_id=org_id,
                    entity_type="object",
                    display_name=display_name,
                    source_system="salesforce",
                    source_record_id=record_id or display_name,
                    run_id=run_id,
                    metadata={
                        "source": collection_name,
                        "record_type": attrs.get("type") or collection_name.rstrip("s"),
                    },
                )
                _append_entity(entities, e)
            except Exception as exc:
                logger.warning(
                    "SF object extraction failed for %s in %s: %s",
                    display_name,
                    collection_name,
                    exc,
                )

    # Team: Salesforce team fields from case/account/opportunity team payloads.
    for collection_name in (
        "teams",
        "team_fields",
        "team_members",
        "case_teams",
        "account_teams",
        "opportunity_teams",
    ):
        for team_record in _iter_records(sf_data.get(collection_name)):
            team_name = _first_record_str(
                team_record,
                (
                    "team_name",
                    "TeamName",
                    "name",
                    "Name",
                    "team",
                    "Team",
                    "group_name",
                    "TeamRole",
                    "team_role",
                    "role",
                    "Role",
                ),
            )
            if not team_name:
                continue
            team_id = _first_record_str(
                team_record,
                ("id", "Id", "team_id", "TeamId", "group_id", "GroupId"),
            )
            try:
                e = resolve_or_create_entity(
                    org_id=org_id,
                    entity_type="team",
                    display_name=team_name,
                    source_system="salesforce",
                    source_record_id=team_id,
                    run_id=run_id,
                    metadata={"source": collection_name},
                )
                _append_entity(entities, e)
            except Exception as exc:
                logger.warning("SF team extraction failed for %s: %s", team_name, exc)

    # Object: sample_case_ids from case_metrics
    for case_id in (sf_data.get("case_metrics") or {}).get("sample_case_ids", []) or []:
        name = _safe_str(case_id)
        if not name:
            continue
        try:
            e = resolve_or_create_entity(
                org_id=org_id,
                entity_type="object",
                display_name=name,
                source_system="salesforce",
                source_record_id=name,
                run_id=run_id,
                metadata={"record_type": "case"},
            )
            _append_entity(entities, e)
        except Exception as exc:
            logger.warning("SF object extraction failed for %s: %s", name, exc)

    # nCino: loan and portfolio references from sf_data["ncino"].
    ncino = sf_data.get("ncino") or {}
    for key in ("loans", "loan_applications", "loan_portfolios"):
        for record in ncino.get(key, []) or []:
            record_id = _first_record_str(
                record,
                (
                    "id",
                    "Id",
                    "loan_id",
                    "portfolio_id",
                    "PortfolioId",
                    "loan_portfolio_id",
                    "loanPortfolioId",
                ),
            )
            owner_id = _safe_str(record.get("OwnerId") or record.get("owner_id"))
            if owner_id:
                try:
                    e = resolve_or_create_entity(
                        org_id=org_id,
                        entity_type="person",
                        display_name=owner_id,
                        source_system="salesforce",
                        source_record_id=owner_id,
                        run_id=run_id,
                        metadata={"source": "ncino_owner"},
                    )
                    _append_entity(entities, e)
                except Exception as exc:
                    logger.warning("nCino person extraction failed for %s: %s", owner_id, exc)

            object_name = _first_record_str(
                record,
                ("Name", "name", "loan_number", "Id", "id", "loan_id"),
            )
            if object_name:
                try:
                    e = resolve_or_create_entity(
                        org_id=org_id,
                        entity_type="object",
                        display_name=object_name,
                        source_system="salesforce",
                        source_record_id=record_id or object_name,
                        run_id=run_id,
                        metadata={"source": f"ncino_{key}", "record_type": "loan"},
                    )
                    _append_entity(entities, e)
                except Exception as exc:
                    logger.warning("nCino object extraction failed for %s: %s", object_name, exc)

            # Project: bounded nCino work/portfolio. Prefer portfolio name, then ID.
            project_name = _first_record_str(
                record,
                (
                    "portfolio_name",
                    "loan_portfolio_name",
                    "loanPortfolioName",
                    "portfolio",
                    "loan_portfolio",
                    "name",
                    "Name",
                    "application_name",
                    "loan_name",
                    "loan_number",
                    "id",
                    "loan_id",
                ),
            )
            if project_name:
                project_id = _first_record_str(
                    record,
                    (
                        "portfolio_id",
                        "PortfolioId",
                        "loan_portfolio_id",
                        "loanPortfolioId",
                        "id",
                        "loan_id",
                    ),
                )
                try:
                    e = resolve_or_create_entity(
                        org_id=org_id,
                        entity_type="project",
                        display_name=project_name,
                        source_system="salesforce",
                        source_record_id=project_id,
                        run_id=run_id,
                        metadata={"source": f"ncino_{key}"},
                    )
                    _append_entity(entities, e)
                except Exception as exc:
                    logger.warning("nCino project extraction failed for %s: %s", project_name, exc)

    return entities


def _extract_jira_entities(
    *,
    org_id: str,
    run_id: str,
    jira_data: Dict[str, Any],
) -> List[Entity]:
    """Extract Person, Team, and Object entities from Jira ingestor output."""
    entities: List[Entity] = []
    issue_metrics = jira_data.get("issue_metrics") or {}

    # Team: project name (one per Jira project in the fixture)
    project_name = _safe_str(issue_metrics.get("project"))
    if project_name:
        try:
            e = resolve_or_create_entity(
                org_id=org_id,
                entity_type="team",
                display_name=project_name,
                source_system="jira",
                source_record_id=None,
                run_id=run_id,
                metadata={"source": "jira_project"},
            )
            _append_entity(entities, e)
        except Exception as exc:
            logger.warning("Jira team extraction failed for %s: %s", project_name, exc)

        try:
            e = resolve_or_create_entity(
                org_id=org_id,
                entity_type="project",
                display_name=project_name,
                source_system="jira",
                source_record_id=_safe_str(issue_metrics.get("project_key")),
                run_id=run_id,
                metadata={"source": "jira_project"},
            )
            _append_entity(entities, e)
        except Exception as exc:
            logger.warning("Jira project extraction failed for %s: %s", project_name, exc)

    # Issues: Object (issue key), Person (assignee, reporter)
    for issue in issue_metrics.get("issues", []) or []:
        issue_project_name, issue_project_id = _ref_name_and_id(issue.get("project"))
        if issue_project_name:
            try:
                e = resolve_or_create_entity(
                    org_id=org_id,
                    entity_type="project",
                    display_name=issue_project_name,
                    source_system="jira",
                    source_record_id=issue_project_id,
                    run_id=run_id,
                    metadata={"source": "jira_issue_project"},
                )
                _append_entity(entities, e)
            except Exception as exc:
                logger.warning(
                    "Jira issue project extraction failed for %s: %s",
                    issue_project_name,
                    exc,
                )

        epic_value = (
            issue.get("epic")
            or issue.get("epic_name")
            or issue.get("epicName")
            or issue.get("epic_key")
            or issue.get("epicKey")
        )
        epic_name, epic_id = _ref_name_and_id(epic_value)
        if epic_name:
            try:
                e = resolve_or_create_entity(
                    org_id=org_id,
                    entity_type="project",
                    display_name=epic_name,
                    source_system="jira",
                    source_record_id=epic_id,
                    run_id=run_id,
                    metadata={"source": "jira_epic"},
                )
                _append_entity(entities, e)
            except Exception as exc:
                logger.warning("Jira epic extraction failed for %s: %s", epic_name, exc)

        issue_key = _safe_str(issue.get("key") or issue.get("id"))
        if issue_key:
            try:
                e = resolve_or_create_entity(
                    org_id=org_id,
                    entity_type="object",
                    display_name=issue_key,
                    source_system="jira",
                    source_record_id=issue_key,
                    run_id=run_id,
                    metadata={
                        "record_type": "issue",
                        "status": issue.get("status"),
                        "project": issue.get("project"),
                    },
                )
                _append_entity(entities, e)
            except Exception as exc:
                logger.warning("Jira object extraction failed for %s: %s", issue_key, exc)

        # Person: assignee.displayName — name-based, confidence=0.8
        assignee = issue.get("assignee") or {}
        if isinstance(assignee, dict):
            assignee_name = _safe_str(assignee.get("displayName") or assignee.get("name"))
        else:
            assignee_name = _safe_str(assignee)
        if assignee_name:
            try:
                e = resolve_or_create_entity(
                    org_id=org_id,
                    entity_type="person",
                    display_name=assignee_name,
                    source_system="jira",
                    source_record_id=None,  # no stable cross-system ID from display name
                    run_id=run_id,
                )
                _append_entity(entities, e)
            except Exception as exc:
                logger.warning("Jira assignee extraction failed for %s: %s", assignee_name, exc)

        # Person: reporter.displayName — name-based, confidence=0.8
        reporter = issue.get("reporter") or {}
        if isinstance(reporter, dict):
            reporter_name = _safe_str(reporter.get("displayName") or reporter.get("name"))
        else:
            reporter_name = _safe_str(reporter)
        if reporter_name:
            try:
                e = resolve_or_create_entity(
                    org_id=org_id,
                    entity_type="person",
                    display_name=reporter_name,
                    source_system="jira",
                    source_record_id=None,
                    run_id=run_id,
                )
                _append_entity(entities, e)
            except Exception as exc:
                logger.warning("Jira reporter extraction failed for %s: %s", reporter_name, exc)

        escalation_target = issue.get("escalated_to") or issue.get("escalation_target")
        escalation_name, escalation_id = _ref_name_and_id(escalation_target)
        if escalation_name:
            target_type = os.getenv("JIRA_ESCALATION_TARGET_TYPE", "person").strip().lower()
            if target_type not in {"person", "team"}:
                target_type = "person"
            try:
                e = resolve_or_create_entity(
                    org_id=org_id,
                    entity_type=target_type,
                    display_name=escalation_name,
                    source_system="jira",
                    source_record_id=escalation_id,
                    run_id=run_id,
                    metadata={"source": "jira_escalation_target"},
                )
                _append_entity(entities, e)
            except Exception as exc:
                logger.warning(
                    "Jira escalation target extraction failed for %s: %s",
                    escalation_name,
                    exc,
                )

    # Sample cross-references: Object from issue_key
    for ref in issue_metrics.get("sample_cross_references", []) or []:
        issue_key = _safe_str(ref.get("issue_key"))
        if issue_key:
            try:
                e = resolve_or_create_entity(
                    org_id=org_id,
                    entity_type="object",
                    display_name=issue_key,
                    source_system="jira",
                    source_record_id=issue_key,
                    run_id=run_id,
                    metadata={
                        "record_type": "issue",
                        "sf_reference": ref.get("sf_reference"),
                    },
                )
                _append_entity(entities, e)
            except Exception as exc:
                logger.warning("Jira cross-ref extraction failed for %s: %s", issue_key, exc)

    return entities


def _extract_servicenow_entities(
    *,
    org_id: str,
    run_id: str,
    sn_data: Dict[str, Any],
) -> List[Entity]:
    """Extract Person, Team, Project, and Object entities from ServiceNow output."""
    entities: List[Entity] = []

    # Team: assignment_groups — names frequently overlap with Jira project names;
    # entity_type='team' vs entity_type='team' prevents false merge here because
    # entity_type is the same, but they come from different source_systems.
    # The canonical_name lookup is scoped to (org_id, entity_type, canonical_name).
    for ag in sn_data.get("assignment_groups", []) or []:
        group_name = _safe_str(ag.get("group_name"))
        if not group_name:
            continue
        try:
            e = resolve_or_create_entity(
                org_id=org_id,
                entity_type="team",
                display_name=group_name,
                source_system="servicenow",
                source_record_id=None,
                run_id=run_id,
                metadata={
                    "source": "assignment_group",
                    "incident_count": ag.get("incident_count"),
                },
            )
            _append_entity(entities, e)
        except Exception as exc:
            logger.warning("SN team extraction failed for %s: %s", group_name, exc)

    for project_record in _iter_records(sn_data.get("projects")):
        project_name = _first_record_str(
            project_record,
            ("display_value", "displayValue", "name", "Name", "project_name", "number", "id"),
        )
        if not project_name:
            continue
        project_id = _first_record_str(project_record, ("sys_id", "id", "Id", "number"))
        try:
            e = resolve_or_create_entity(
                org_id=org_id,
                entity_type="project",
                display_name=project_name,
                source_system="servicenow",
                source_record_id=project_id,
                run_id=run_id,
                metadata={"source": "servicenow_project"},
            )
            _append_entity(entities, e)
        except Exception as exc:
            logger.warning("SN project extraction failed for %s: %s", project_name, exc)

    # Incidents: Object (incident number), Person (assigned_to, caller_id)
    incident_metrics = sn_data.get("incident_metrics") or {}
    for incident in incident_metrics.get("incidents", []) or []:
        project_name, project_id = _ref_name_and_id(incident.get("project"))
        if project_name:
            try:
                e = resolve_or_create_entity(
                    org_id=org_id,
                    entity_type="project",
                    display_name=project_name,
                    source_system="servicenow",
                    source_record_id=project_id,
                    run_id=run_id,
                    metadata={"source": "servicenow_incident_project"},
                )
                _append_entity(entities, e)
            except Exception as exc:
                logger.warning(
                    "SN incident project extraction failed for %s: %s",
                    project_name,
                    exc,
                )

        inc_number = _safe_str(incident.get("number") or incident.get("id"))
        if inc_number:
            try:
                e = resolve_or_create_entity(
                    org_id=org_id,
                    entity_type="object",
                    display_name=inc_number,
                    source_system="servicenow",
                    source_record_id=inc_number,
                    run_id=run_id,
                    metadata={
                        "record_type": "incident",
                        "state": incident.get("state"),
                        "priority": incident.get("priority"),
                    },
                )
                _append_entity(entities, e)
            except Exception as exc:
                logger.warning("SN object extraction failed for %s: %s", inc_number, exc)

        # Person: assigned_to.display_value — name-based, confidence=0.8
        assigned_to = incident.get("assigned_to") or {}
        if isinstance(assigned_to, dict):
            assigned_name = _safe_str(
                assigned_to.get("display_value") or assigned_to.get("value")
            )
        else:
            assigned_name = _safe_str(assigned_to)
        if assigned_name:
            try:
                e = resolve_or_create_entity(
                    org_id=org_id,
                    entity_type="person",
                    display_name=assigned_name,
                    source_system="servicenow",
                    source_record_id=None,  # name-based only
                    run_id=run_id,
                )
                _append_entity(entities, e)
            except Exception as exc:
                logger.warning("SN assigned_to extraction failed for %s: %s", assigned_name, exc)

        assignment_group_name, assignment_group_id = _ref_name_and_id(
            incident.get("assignment_group")
        )
        if assignment_group_name:
            try:
                e = resolve_or_create_entity(
                    org_id=org_id,
                    entity_type="team",
                    display_name=assignment_group_name,
                    source_system="servicenow",
                    source_record_id=assignment_group_id,
                    run_id=run_id,
                    metadata={"source": "servicenow_incident_assignment_group"},
                )
                _append_entity(entities, e)
            except Exception as exc:
                logger.warning(
                    "SN assignment group extraction failed for %s: %s",
                    assignment_group_name,
                    exc,
                )

        # Person: caller_id.display_value
        caller_id = incident.get("caller_id") or {}
        if isinstance(caller_id, dict):
            caller_name = _safe_str(
                caller_id.get("display_value") or caller_id.get("value")
            )
        else:
            caller_name = _safe_str(caller_id)
        if caller_name:
            try:
                e = resolve_or_create_entity(
                    org_id=org_id,
                    entity_type="person",
                    display_name=caller_name,
                    source_system="servicenow",
                    source_record_id=None,
                    run_id=run_id,
                )
                _append_entity(entities, e)
            except Exception as exc:
                logger.warning("SN caller_id extraction failed for %s: %s", caller_name, exc)

        escalation_name, escalation_id = _ref_name_and_id(incident.get("escalated_to"))
        if escalation_name:
            target_type = os.getenv(
                "SERVICENOW_ESCALATION_TARGET_TYPE", "person"
            ).strip().lower()
            if target_type not in {"person", "team"}:
                target_type = "person"
            try:
                e = resolve_or_create_entity(
                    org_id=org_id,
                    entity_type=target_type,
                    display_name=escalation_name,
                    source_system="servicenow",
                    source_record_id=escalation_id,
                    run_id=run_id,
                    metadata={"source": "servicenow_escalation_target"},
                )
                _append_entity(entities, e)
            except Exception as exc:
                logger.warning(
                    "SN escalation target extraction failed for %s: %s",
                    escalation_name,
                    exc,
                )

    return entities


def _extract_catalog_system_entities(
    *,
    org_id: str,
    run_id: str,
    catalog_data: Dict[str, Any],
) -> List[Entity]:
    """Extract System entities from Integration Hub/workspace catalog payloads."""
    entities: List[Entity] = []
    seen: set[str] = set()

    for collection_name in (
        "connectors",
        "systems",
        "workspace_catalog",
        "integration_hub",
    ):
        raw_collection = catalog_data.get(collection_name)
        records = _iter_records(raw_collection)
        if isinstance(raw_collection, dict):
            for nested_name in ("connectors", "systems", "workspace_catalog"):
                records.extend(_iter_records(raw_collection.get(nested_name)))
            if not records:
                records.extend(
                    item for item in raw_collection.values() if isinstance(item, dict)
                )

        for record in records:
            display_name = _first_record_str(
                record,
                ("name", "Name", "display_name", "connector_name", "source", "signal_source", "id"),
            )
            if not display_name:
                continue
            connector_id = _first_record_str(
                record,
                ("connector_id", "id", "Id", "source", "signal_source", "key"),
            ) or display_name
            dedupe_key = connector_id.strip().lower()
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            try:
                e = resolve_or_create_entity(
                    org_id=org_id,
                    entity_type="system",
                    display_name=display_name,
                    source_system="integration_hub",
                    source_record_id=connector_id,
                    run_id=run_id,
                    metadata={"source": collection_name, "connector_id": connector_id},
                )
                _append_entity(entities, e)
            except Exception as exc:
                logger.warning("Catalog system extraction failed for %s: %s", display_name, exc)

    return entities


def _extract_detector_entities(
    *,
    org_id: str,
    run_id: str,
    pack_id: str,
    detector_results: List[Any],
) -> List[Entity]:
    """Extract System and Process entities from detector results.

    System and Process entities use stable AgentIQ identifiers → confidence=1.0.
    One System entity per distinct signal_source; one Process entity per
    distinct detector_id.
    """
    entities: List[Entity] = []
    seen_systems: set[str] = set()
    seen_processes: set[str] = set()

    for dr in detector_results or []:
        # System entity per distinct signal_source value
        signal_source = _safe_str(getattr(dr, "signal_source", None))
        if signal_source and signal_source not in seen_systems:
            seen_systems.add(signal_source)
            try:
                e = resolve_or_create_entity(
                    org_id=org_id,
                    entity_type="system",
                    display_name=signal_source,
                    source_system=signal_source,
                    source_record_id=signal_source,  # connector_id per spec
                    run_id=run_id,
                    metadata={"connector_id": signal_source, "pack_id": pack_id},
                )
                _append_entity(entities, e)
            except Exception as exc:
                logger.warning(
                    "System entity extraction failed for %s: %s", signal_source, exc
                )

        # Process entity per distinct detector_id value
        detector_id = _safe_str(getattr(dr, "detector_id", None))
        if detector_id and detector_id not in seen_processes:
            seen_processes.add(detector_id)
            try:
                e = resolve_or_create_entity(
                    org_id=org_id,
                    entity_type="process",
                    display_name=detector_id,
                    source_system="agentiq",
                    source_record_id=detector_id,
                    run_id=run_id,
                    metadata={"pack_id": pack_id, "detector_id": detector_id},
                )
                _append_entity(entities, e)
            except Exception as exc:
                logger.warning(
                    "Process entity extraction failed for %s: %s", detector_id, exc
                )

    return entities


# ---------------------------------------------------------------------------
# ENT-1 — Overlay-driven extraction (Task T4)
#
# Overlay extraction is ADDITIVE: it runs AFTER the default per-source
# extraction and contributes extra customer-specific entities (union). Default
# extraction always runs, so fields not covered by the overlay still fall back
# to default behavior (AC1). Overlay person rules with resolution_source='id'
# pass a source_record_id, which the resolver scores at confidence 1.0 (AC3),
# taking precedence over a weaker name-based default sighting via the resolver's
# confidence-upgrade (MAX) logic.
# ---------------------------------------------------------------------------

# Generic record buckets whose entries carry their own object type (rather than
# being keyed by object API name). Salesforce REST returns records with an
# {"attributes": {"type": "LLC_BI__Loan__c"}} envelope.
_TYPED_RECORD_BUCKETS = ("records", "objects", "sample_records")


def _record_object_type(record: Dict[str, Any]) -> Optional[str]:
    """Return the object API name a record declares about itself, or None.

    Supports the Salesforce REST ``attributes.type`` envelope and a few common
    plain-key variants. Used to match generic-bucket records to overlay rules.
    """
    attrs = record.get("attributes")
    if isinstance(attrs, dict):
        t = _safe_str(attrs.get("type"))
        if t:
            return t
    for key in ("object_api_name", "type", "sobject_type", "Type"):
        t = _safe_str(record.get(key))
        if t:
            return t
    return None


def _index_records_by_object_type(
    connector_data: Dict[str, Any],
    wanted_types: set[str],
) -> Dict[str, List[Dict[str, Any]]]:
    """Index a connector's records by object API name, limited to wanted_types.

    Two discovery conventions are supported so overlays work with realistic
    ingestor shapes:
      1. Direct keying: ``connector_data["LLC_BI__Loan__c"] = [ {...}, ... ]``.
      2. Typed records in a generic bucket (``records`` / ``objects`` /
         ``sample_records``) carrying ``attributes.type`` (Salesforce REST).
    The nCino nested bucket (``connector_data["ncino"]``) is scanned the same way.
    Records are de-duplicated by stable source identifiers first, then by a
    canonical JSON fingerprint. This prevents the same Salesforce record from
    being extracted twice when it appears under both direct and typed buckets.
    """
    index: Dict[str, List[Dict[str, Any]]] = {t: [] for t in wanted_types}
    seen: Dict[str, set[str]] = {t: set() for t in wanted_types}

    def _record_dedup_key(obj_type: str, record: Dict[str, Any]) -> str:
        stable_id = _first_record_str(
            record,
            (
                "Id",
                "id",
                "sys_id",
                "record_id",
                "recordId",
                "loan_id",
                "portfolio_id",
                "number",
                "key",
            ),
        )
        if stable_id:
            return f"id:{obj_type}:{stable_id}"
        try:
            return f"json:{obj_type}:{json.dumps(record, sort_keys=True, default=str)}"
        except TypeError:
            return f"repr:{obj_type}:{repr(record)}"

    def _add(obj_type: Optional[str], record: Any) -> None:
        if obj_type in index and isinstance(record, dict):
            dedup_key = _record_dedup_key(obj_type, record)
            if dedup_key not in seen[obj_type]:
                seen[obj_type].add(dedup_key)
                index[obj_type].append(record)

    def _scan(container: Dict[str, Any]) -> None:
        # 1. direct keying by object API name
        for obj_type in wanted_types:
            for record in _iter_records(container.get(obj_type)):
                _add(obj_type, record)
        # 2. typed records in generic buckets
        for bucket in _TYPED_RECORD_BUCKETS:
            for record in _iter_records(container.get(bucket)):
                _add(_record_object_type(record), record)

    if isinstance(connector_data, dict):
        _scan(connector_data)
        ncino = connector_data.get("ncino")
        if isinstance(ncino, dict):
            _scan(ncino)

    return index


def _overlay_emit(entities: List[Entity], *, label: str, **kwargs: Any) -> None:
    """Resolve+append one overlay entity, honoring the service-account filter.

    A filtered service account returns None (already counted) and is silently
    skipped. Any resolver error is logged and swallowed — overlay extraction is
    non-blocking, exactly like the default path.
    """
    try:
        entity = resolve_or_create_entity(**kwargs)
        _append_entity(entities, entity)
    except Exception as exc:
        logger.warning(
            "overlay %s extraction failed for %s: %s",
            label,
            kwargs.get("display_name"),
            exc,
        )


def _apply_overlay_rules(
    *,
    org_id: str,
    run_id: str,
    connector_id: str,
    overlay: Any,
    connector_data: Dict[str, Any],
) -> List[Entity]:
    """Apply one overlay's person/team/object rules to a connector's data.

    Returns the extra entities the overlay contributed. Never raises — the
    caller treats overlay extraction as additive and non-blocking.
    """
    entities: List[Entity] = []
    if not connector_data:
        return entities

    records_by_type = _index_records_by_object_type(
        connector_data, overlay.referenced_object_names()
    )

    # Person rules — ID-based fields resolve at confidence 1.0 (AC3).
    for rule in overlay.person_fields:
        use_id = rule.resolution_source == "id"
        for record in records_by_type.get(rule.object_api_name, []):
            display_name, source_id = _ref_name_and_id(record.get(rule.field_api_name))
            if not display_name:
                continue
            _overlay_emit(
                entities,
                label="person",
                org_id=org_id,
                entity_type="person",
                display_name=display_name,
                source_system=connector_id,
                source_record_id=(source_id or display_name) if use_id else None,
                run_id=run_id,
                metadata={
                    "overlay_version": overlay.version,
                    "label": rule.label,
                    "object": rule.object_api_name,
                    "field": rule.field_api_name,
                    "sources": [connector_id],
                },
            )

    # Team rules.
    for rule in overlay.team_fields:
        use_id = rule.resolution_source == "id"
        for record in records_by_type.get(rule.object_api_name, []):
            display_name, source_id = _ref_name_and_id(record.get(rule.field_api_name))
            if not display_name:
                continue
            _overlay_emit(
                entities,
                label="team",
                org_id=org_id,
                entity_type="team",
                display_name=display_name,
                source_system=connector_id,
                source_record_id=(source_id or display_name) if use_id else None,
                run_id=run_id,
                metadata={
                    "overlay_version": overlay.version,
                    "label": rule.label,
                    "object": rule.object_api_name,
                    "field": rule.field_api_name,
                    "sources": [connector_id],
                },
            )

    # Object rules.
    for rule in overlay.object_rules:
        for record in records_by_type.get(rule.object_api_name, []):
            display_name = _first_record_str(
                record, (rule.name_field, "Name", "name", "Id", "id")
            )
            if not display_name:
                continue
            source_id = _first_record_str(record, ("Id", "id", "sys_id"))
            _overlay_emit(
                entities,
                label="object",
                org_id=org_id,
                entity_type=rule.entity_type,
                display_name=display_name,
                source_system=connector_id,
                source_record_id=source_id,
                run_id=run_id,
                metadata={
                    "overlay_version": overlay.version,
                    "record_type": rule.record_type,
                    "object": rule.object_api_name,
                    "sources": [connector_id],
                },
            )

    return entities


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def extract_entities(
    *,
    org_id: str,
    run_id: str,
    pack_id: str,
    detector_results: List[Any],
    ingestor_data: Dict[str, Any],
) -> List[Entity]:
    """Extract structured entities from ingestor output and detector results.

    Called from runner.py after snapshot_signals(), before LLM enrichment.
    Never raises — all failures are caught, logged, and the run continues.

    Args:
        org_id:           Workspace identifier — all entities scoped to this.
        run_id:           Current discovery run ID.
        pack_id:          Active pack (service_cloud / ncino / strs_benefits).
        detector_results: List of DetectorResult objects from the detector phase.
        ingestor_data:    Dict keyed by connector name:
                            {"salesforce": sf_data, "servicenow": sn_data, "jira": jira_data}

    Returns:
        List of extracted Entity objects (may include existing rows updated
        with incremented run_count, not only newly created rows).
    """
    all_entities: List[Entity] = []
    failure_count = 0

    # ENT-1: resolve any overlays registered for this run's connectors. Default
    # extraction ALWAYS runs; overlays add customer-specific entities on top
    # (union — AC1). When no overlay is registered the context is None and the
    # resolve wrapper is a transparent pass-through (AC2). Lookup never blocks.
    overlay_by_connector: Dict[str, Any] = {}
    try:
        from app.entity_overlays.overlay_registry import get_overlay

        for _connector in ("salesforce", "jira", "servicenow"):
            _ov = get_overlay(org_id, _connector)
            if _ov is not None:
                overlay_by_connector[_connector] = _ov
    except Exception as exc:
        logger.warning("entity overlay lookup failed (non-blocking): %s", exc)

    # One service-account filter for the whole extraction, built from the union
    # of every active overlay's patterns. Setting the context (even with empty
    # patterns) also enables cross-system provenance tracking (metadata.sources).
    overlay_ctx: Optional[_OverlayContext] = None
    if overlay_by_connector:
        _sa_patterns: List[str] = []
        for _ov in overlay_by_connector.values():
            _sa_patterns.extend(_ov.service_account_patterns or [])
        overlay_ctx = _OverlayContext(_sa_patterns)
    ctx_token = _overlay_context.set(overlay_ctx)

    try:
        # Salesforce / nCino — default extraction
        sf_data = ingestor_data.get("salesforce") or {}
        if sf_data:
            try:
                batch = _extract_salesforce_entities(
                    org_id=org_id, run_id=run_id, sf_data=sf_data
                )
                all_entities.extend(batch)
                logger.info("Entity extraction — Salesforce: %d entities", len(batch))
            except Exception as exc:
                failure_count += 1
                logger.warning("Salesforce entity extraction failed: %s", exc)

            # Overlay extraction for Salesforce/nCino (additive union)
            if "salesforce" in overlay_by_connector:
                try:
                    batch = _apply_overlay_rules(
                        org_id=org_id,
                        run_id=run_id,
                        connector_id="salesforce",
                        overlay=overlay_by_connector["salesforce"],
                        connector_data=sf_data,
                    )
                    all_entities.extend(batch)
                    logger.info(
                        "Entity extraction — Salesforce overlay: %d entities", len(batch)
                    )
                except Exception as exc:
                    failure_count += 1
                    logger.warning("Salesforce overlay extraction failed: %s", exc)

        # Jira — default extraction
        jira_data = ingestor_data.get("jira") or {}
        if jira_data:
            try:
                batch = _extract_jira_entities(
                    org_id=org_id, run_id=run_id, jira_data=jira_data
                )
                all_entities.extend(batch)
                logger.info("Entity extraction — Jira: %d entities", len(batch))
            except Exception as exc:
                failure_count += 1
                logger.warning("Jira entity extraction failed: %s", exc)

            # Overlay extraction for Jira (additive union)
            if "jira" in overlay_by_connector:
                try:
                    batch = _apply_overlay_rules(
                        org_id=org_id,
                        run_id=run_id,
                        connector_id="jira",
                        overlay=overlay_by_connector["jira"],
                        connector_data=jira_data,
                    )
                    all_entities.extend(batch)
                    logger.info("Entity extraction — Jira overlay: %d entities", len(batch))
                except Exception as exc:
                    failure_count += 1
                    logger.warning("Jira overlay extraction failed: %s", exc)

        # ServiceNow — default extraction
        sn_data = ingestor_data.get("servicenow") or {}
        if sn_data:
            try:
                batch = _extract_servicenow_entities(
                    org_id=org_id, run_id=run_id, sn_data=sn_data
                )
                all_entities.extend(batch)
                logger.info("Entity extraction — ServiceNow: %d entities", len(batch))
            except Exception as exc:
                failure_count += 1
                logger.warning("ServiceNow entity extraction failed: %s", exc)

            # Overlay extraction for ServiceNow (additive union)
            if "servicenow" in overlay_by_connector:
                try:
                    batch = _apply_overlay_rules(
                        org_id=org_id,
                        run_id=run_id,
                        connector_id="servicenow",
                        overlay=overlay_by_connector["servicenow"],
                        connector_data=sn_data,
                    )
                    all_entities.extend(batch)
                    logger.info(
                        "Entity extraction — ServiceNow overlay: %d entities", len(batch)
                    )
                except Exception as exc:
                    failure_count += 1
                    logger.warning("ServiceNow overlay extraction failed: %s", exc)

        # Detector-level: System and Process (stable identifiers)
        try:
            batch = _extract_detector_entities(
                org_id=org_id,
                run_id=run_id,
                pack_id=pack_id,
                detector_results=detector_results,
            )
            all_entities.extend(batch)
            logger.info("Entity extraction — detector entities: %d", len(batch))
        except Exception as exc:
            failure_count += 1
            logger.warning("Detector entity extraction failed: %s", exc)

        # Integration Hub / workspace catalog: System entities even when no detector fires.
        catalog_data = {
            key: value
            for key, value in ingestor_data.items()
            if key in {"connectors", "systems", "workspace_catalog", "integration_hub"}
        }
        if catalog_data:
            try:
                batch = _extract_catalog_system_entities(
                    org_id=org_id,
                    run_id=run_id,
                    catalog_data=catalog_data,
                )
                all_entities.extend(batch)
                logger.info("Entity extraction — catalog systems: %d", len(batch))
            except Exception as exc:
                failure_count += 1
                logger.warning("Catalog system extraction failed: %s", exc)
    finally:
        # Always detach the overlay context so it never leaks into the next
        # extraction sharing this execution context.
        _overlay_context.reset(ctx_token)

    # A source payload can reference the same owner once per Case or loan.
    # Resolution maps those occurrences to one row; downstream consumers should
    # receive that canonical entity only once.
    all_entities = _deduplicate_entities(all_entities)

    # Service accounts filtered (T5 / AC5) — distinct identities skipped by the
    # active overlay's service_account_patterns. 0 when no overlay is active.
    filtered_service_account_count = (
        overlay_ctx.filtered_count if overlay_ctx is not None else 0
    )

    ambiguous_count = sum(
        1 for e in all_entities if e.resolution_status == "ambiguous"
    )

    logger.info(
        "Entity extraction complete — run=%s total=%d ambiguous=%d failures=%d",
        run_id,
        len(all_entities),
        ambiguous_count,
        failure_count,
    )

    # Fire-and-forget telemetry — never raises.
    # Not emitted if extraction raised an exception; the runner warning covers that.
    # PII GUARD: this payload carries COUNTS and identifiers (run/org/pack) only.
    # Never add canonical_name or display_name here — those can be real user
    # names (e.g. "Alice Smith") and telemetry must not log sensitive values.
    try:
        from app.telemetry import record_event
        record_event(
            "entity.extraction_completed",
            {
                "run_id": run_id,
                "org_id": org_id,
                "pack_id": pack_id,
                "source": "entity_extractor",
                "entity_count": len(all_entities),
                "ambiguous_count": ambiguous_count,
                "failure_count": failure_count,
                # ENT-1 / AC5: number of service-account identities filtered by
                # the active overlay (0 when no overlay is active).
                "filtered_service_account_count": filtered_service_account_count,
            },
        )
    except Exception as exc:
        logger.warning("entity.extraction_completed telemetry failed: %s", exc)

    # Persist filtered entity summaries in run KV for the OppEnrichment endpoint.
    # Section 8: entities with run_count < 3 are service accounts — excluded from
    # evidence trace display, retained in the DB for graph completeness.
    try:
        from app import db
        entity_summaries = [
            {
                "entity_id": str(e.id),
                "entity_type": e.entity_type,
                "display_name": e.display_name,
                "source_system": e.source_system,
                "resolution_confidence": e.resolution_confidence,
                "resolution_status": e.resolution_status,
                "run_count": e.run_count,
            }
            for e in all_entities
            if e.run_count >= _MIN_DISPLAY_RUN_COUNT
        ]
        db.run_kv_set("entities", run_id, entity_summaries)
    except Exception as exc:
        logger.warning("Entity KV persistence failed (non-blocking): %s", exc)

    return all_entities
