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

import logging
from typing import Any, Dict, List, Optional

from app.entity_resolution import resolve_or_create_entity
from database.models.entities import Entity

logger = logging.getLogger(__name__)

_MIN_DISPLAY_RUN_COUNT = 3  # entities below this threshold treated as service accounts


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
                entities.append(e)
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
                    entities.append(e)
                except Exception as exc:
                    logger.warning(
                        "SF %s extraction failed for %s: %s",
                        field_name,
                        display_name,
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
                entities.append(e)
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
            entities.append(e)
        except Exception as exc:
            logger.warning("SF object extraction failed for %s: %s", name, exc)

    # nCino: loan portfolio references from sf_data["ncino"]
    ncino = sf_data.get("ncino") or {}
    for key in ("loan_applications", "loan_portfolios"):
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
                    entities.append(e)
                except Exception as exc:
                    logger.warning("nCino person extraction failed for %s: %s", owner_id, exc)

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
                    entities.append(e)
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
            entities.append(e)
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
            entities.append(e)
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
                entities.append(e)
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
                entities.append(e)
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
                entities.append(e)
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
                entities.append(e)
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
                entities.append(e)
            except Exception as exc:
                logger.warning("Jira reporter extraction failed for %s: %s", reporter_name, exc)

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
                entities.append(e)
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
            entities.append(e)
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
            entities.append(e)
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
                entities.append(e)
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
                entities.append(e)
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
                entities.append(e)
            except Exception as exc:
                logger.warning("SN assigned_to extraction failed for %s: %s", assigned_name, exc)

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
                entities.append(e)
            except Exception as exc:
                logger.warning("SN caller_id extraction failed for %s: %s", caller_name, exc)

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
                entities.append(e)
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
                entities.append(e)
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
                entities.append(e)
            except Exception as exc:
                logger.warning(
                    "Process entity extraction failed for %s: %s", detector_id, exc
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

    # Salesforce / nCino
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

    # Jira
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

    # ServiceNow
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
