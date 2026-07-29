"""Conservative ServiceNow incident-to-CI resolution for MSP-B3.

Only explicit ServiceNow references are accepted.  The incident ``cmdb_ci``
field is authoritative; when it is absent, one unambiguous ``task_ci`` affected
CI may be used.  Names, descriptions, assignment groups, and inferred graph
connections never participate in resolution.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from database.models.entities import Entity


RESOLUTION_VERSION = "servicenow_explicit_ci_v1"


@dataclass(frozen=True)
class ExplicitCIReferenceResult:
    """Outcome of one exact, organization-scoped ServiceNow CI lookup."""

    ci_sys_id: str | None
    entity: Entity | None
    reason: str | None


def _text(value: Any) -> str:
    return str(value or "").strip()


def _explicit_reference_id(value: Any) -> str:
    """Return only a raw ServiceNow reference ID, never a display value."""
    if isinstance(value, dict):
        return _text(value.get("value") or value.get("sys_id"))
    if isinstance(value, str):
        return value.strip()
    return ""


def _reference_is_present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, dict):
        return bool(value)
    return True


def _entity_metadata(entity: Entity) -> dict[str, Any]:
    return entity.metadata if isinstance(entity.metadata, dict) else {}


def _matching_entities(
    *,
    org_id: str,
    ci_sys_id: str,
    entities: Iterable[Entity],
) -> list[Entity]:
    """Return exact current-scope CI matches for this organization."""
    return [
        entity
        for entity in entities
        if entity.org_id == org_id
        and entity.entity_type == "system"
        and entity.source_system == "servicenow"
        and _text(entity.source_record_id) == ci_sys_id
    ]


def resolve_explicit_servicenow_ci_reference(
    *,
    org_id: str,
    reference: Any,
    entities: Iterable[Entity],
) -> ExplicitCIReferenceResult:
    """Resolve one explicit ServiceNow reference without inferred matching.

    This is the shared conservative lookup used by incident and SecOps
    workflow signals.  Display values, names, descriptions, ownership, and
    other textual fields are deliberately ignored.
    """
    if not _reference_is_present(reference):
        return ExplicitCIReferenceResult(None, None, "missing_explicit_reference")

    ci_sys_id = _explicit_reference_id(reference)
    if not ci_sys_id:
        return ExplicitCIReferenceResult(None, None, "invalid_explicit_reference")

    matches = _matching_entities(
        org_id=org_id,
        ci_sys_id=ci_sys_id,
        entities=entities,
    )
    if not matches:
        return ExplicitCIReferenceResult(ci_sys_id, None, "ci_not_in_current_scope")
    if len(matches) != 1 or matches[0].resolution_status != "resolved":
        return ExplicitCIReferenceResult(ci_sys_id, None, "ambiguous_ci_entity")

    entity = matches[0]
    metadata = _entity_metadata(entity)
    if metadata.get("is_retired") is True or _text(
        metadata.get("lifecycle_state")
    ).casefold() == "retired":
        return ExplicitCIReferenceResult(ci_sys_id, None, "retired_ci")
    return ExplicitCIReferenceResult(ci_sys_id, entity, None)


def _base_resolution(incident: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "version": RESOLUTION_VERSION,
        "status": "unresolved",
        "reason": "missing_explicit_reference",
        "method": None,
        "source_system": "servicenow",
        "incident_sys_id": _text(incident.get("sys_id")) or None,
        "incident_number": _text(incident.get("number")) or None,
        "ci_sys_id": None,
        "ci_entity_id": None,
        "source_timestamp": incident.get("source_timestamp"),
        "source_url": incident.get("source_url"),
    }


def _set_unresolved(
    incident: dict[str, Any],
    resolution: dict[str, Any],
    reason: str,
    *,
    method: str | None = None,
    ci_sys_id: str | None = None,
) -> None:
    resolution.update(
        {
            "status": "unresolved",
            "reason": reason,
            "method": method,
            "ci_sys_id": ci_sys_id,
            "ci_entity_id": None,
        }
    )
    incident["ci_resolution"] = resolution
    # Re-evaluation is deterministic and cannot leave a prior successful join
    # behind after the source reference or current class scope changes.
    incident.pop("ci_entity_id", None)
    incident.pop("ci_sys_id", None)
    incident.pop("resolved_ci", None)
    incident.pop("ci_evidence_trace", None)


def observed_ci_dependency_hops(
    *,
    org_id: str,
    ci_sys_id: str,
    entities: list[Entity],
    relationships: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Represent explicit outgoing CMDB edges with provenance for evidence."""
    hops: list[dict[str, Any]] = []
    for relationship in relationships:
        if not isinstance(relationship, Mapping):
            continue
        if _text(relationship.get("source_ci_id")) != ci_sys_id:
            continue
        target_ci_id = _text(relationship.get("target_ci_id"))
        relationship_sys_id = _text(relationship.get("sys_id"))
        relationship_type = _text(relationship.get("relationship_type"))
        if not target_ci_id or not relationship_sys_id or not relationship_type:
            continue
        targets = _matching_entities(
            org_id=org_id,
            ci_sys_id=target_ci_id,
            entities=entities,
        )
        if len(targets) != 1 or targets[0].resolution_status != "resolved":
            continue
        target = targets[0]
        hops.append(
            {
                "origin": "observed",
                "relationship_type": relationship_type,
                "source_system": "servicenow",
                "source_artifact": relationship_sys_id,
                "source_record_id": relationship_sys_id,
                "source_type": relationship.get("source_type"),
                "source_timestamp": relationship.get("source_timestamp"),
                "source_url": relationship.get("source_url"),
                "from_ci_sys_id": ci_sys_id,
                "from_ci_entity_id": None,  # filled by the caller
                "to_ci_sys_id": target_ci_id,
                "to_ci_entity_id": str(target.id),
            }
        )
    return sorted(
        hops,
        key=lambda hop: (
            hop["relationship_type"],
            hop["to_ci_sys_id"],
            hop["source_record_id"],
        ),
    )


def resolve_incident_ci_references(
    *,
    org_id: str,
    incident_metrics: dict[str, Any],
    cmdb_entities: Iterable[Entity],
    cmdb_relationships: Iterable[Mapping[str, Any]] = (),
) -> dict[str, int]:
    """Resolve incident signals to current-org, current-scope CI graph nodes.

    The function mutates each incident signal with a stable ``ci_resolution``
    object.  A resolved signal also receives ``ci_entity_id``, ``resolved_ci``,
    and a provenance-rich two-hop trace that evidence builders can consume.
    """
    entities = list(cmdb_entities)
    relationships = list(cmdb_relationships)
    counts = {"resolved": 0, "unresolved": 0}
    affected_lookup = incident_metrics.get("affected_ci_lookup")
    affected_lookup_status = _text(
        affected_lookup.get("status")
        if isinstance(affected_lookup, Mapping)
        else None
    )

    for incident in incident_metrics.get("incidents", []) or []:
        if not isinstance(incident, dict):
            continue
        resolution = _base_resolution(incident)
        incident_sys_id = _text(incident.get("sys_id"))
        if not incident_sys_id:
            _set_unresolved(incident, resolution, "missing_incident_identifier")
            counts["unresolved"] += 1
            continue

        primary = incident.get("cmdb_ci")
        primary_present = _reference_is_present(primary)
        reference_record: Mapping[str, Any] | None = None
        if primary_present:
            method = "incident_cmdb_ci"
            ci_sys_id = _explicit_reference_id(primary)
            if not ci_sys_id:
                _set_unresolved(
                    incident,
                    resolution,
                    "invalid_primary_reference",
                    method=method,
                )
                counts["unresolved"] += 1
                continue
        else:
            method = "affected_ci_task"
            references = incident.get("affected_ci_references") or []
            valid_references = [
                reference
                for reference in references
                if isinstance(reference, Mapping)
                and _text(reference.get("incident_sys_id")) == incident_sys_id
                and _text(reference.get("relationship_sys_id"))
                and _text(reference.get("ci_sys_id"))
            ]
            unique_ci_ids = sorted(
                {_text(reference.get("ci_sys_id")) for reference in valid_references}
            )
            if not references:
                reason = (
                    "affected_ci_lookup_unavailable"
                    if affected_lookup_status == "unavailable"
                    else "missing_explicit_reference"
                )
                _set_unresolved(incident, resolution, reason)
                counts["unresolved"] += 1
                continue
            if not unique_ci_ids:
                _set_unresolved(
                    incident,
                    resolution,
                    "invalid_affected_ci_reference",
                    method=method,
                )
                counts["unresolved"] += 1
                continue
            if len(unique_ci_ids) != 1:
                resolution["candidate_ci_sys_ids"] = unique_ci_ids
                _set_unresolved(
                    incident,
                    resolution,
                    "ambiguous_affected_ci_reference",
                    method=method,
                )
                counts["unresolved"] += 1
                continue
            ci_sys_id = unique_ci_ids[0]
            reference_record = sorted(
                (
                    reference
                    for reference in valid_references
                    if _text(reference.get("ci_sys_id")) == ci_sys_id
                ),
                key=lambda reference: _text(reference.get("relationship_sys_id")),
            )[0]
            resolution["supporting_relationship_ids"] = sorted(
                {
                    _text(reference.get("relationship_sys_id"))
                    for reference in valid_references
                    if _text(reference.get("ci_sys_id")) == ci_sys_id
                }
            )

        ci_result = resolve_explicit_servicenow_ci_reference(
            org_id=org_id,
            reference=ci_sys_id,
            entities=entities,
        )
        if ci_result.reason is not None or ci_result.entity is None:
            _set_unresolved(
                incident,
                resolution,
                ci_result.reason or "ambiguous_ci_entity",
                method=method,
                ci_sys_id=ci_sys_id,
            )
            counts["unresolved"] += 1
            continue

        entity = ci_result.entity
        metadata = _entity_metadata(entity)

        reference_field = "cmdb_ci" if method == "incident_cmdb_ci" else "task_ci.ci_item"
        source_artifact = incident_sys_id
        source_timestamp = incident.get("source_timestamp")
        source_url = incident.get("source_url")
        if reference_record is not None:
            source_artifact = _text(reference_record.get("relationship_sys_id"))
            source_timestamp = reference_record.get("source_timestamp")
            source_url = reference_record.get("source_url")

        resolution.update(
            {
                "status": "resolved",
                "reason": None,
                "method": method,
                "reference_field": reference_field,
                "ci_sys_id": ci_sys_id,
                "ci_entity_id": str(entity.id),
                "source_timestamp": source_timestamp,
                "source_url": source_url,
            }
        )
        incident_to_ci = {
            "origin": "observed",
            "relationship_type": "references",
            "reference_field": reference_field,
            "source_system": "servicenow",
            "source_artifact": source_artifact,
            "source_record_id": source_artifact,
            "source_timestamp": source_timestamp,
            "source_url": source_url,
            "incident_sys_id": incident_sys_id,
            "incident_source_url": incident.get("source_url"),
            "ci_sys_id": ci_sys_id,
            "ci_entity_id": str(entity.id),
            "ci_source_url": metadata.get("source_url"),
        }
        dependencies = observed_ci_dependency_hops(
            org_id=org_id,
            ci_sys_id=ci_sys_id,
            entities=entities,
            relationships=relationships,
        )
        for hop in dependencies:
            hop["from_ci_entity_id"] = str(entity.id)

        incident["ci_resolution"] = resolution
        incident["ci_sys_id"] = ci_sys_id
        incident["ci_entity_id"] = str(entity.id)
        incident["resolved_ci"] = {
            "entity_id": str(entity.id),
            "source_system": "servicenow",
            "source_record_id": ci_sys_id,
            "display_name": entity.display_name,
            "ci_class": metadata.get("ci_class"),
            "operational_status": metadata.get("operational_status"),
            "lifecycle_state": metadata.get("lifecycle_state"),
            "source_timestamp": metadata.get("source_updated_at"),
            "source_url": metadata.get("source_url"),
        }
        incident["ci_evidence_trace"] = {
            "incident_to_ci": incident_to_ci,
            "ci_dependencies": dependencies,
        }
        counts["resolved"] += 1

    incident_metrics["ci_resolution_summary"] = counts.copy()
    return counts
