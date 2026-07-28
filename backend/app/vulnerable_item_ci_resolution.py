"""Conservative ServiceNow vulnerable-item-to-CI resolution for MSP-B11.

Only the explicit ``sn_vul_vulnerable_item.cmdb_ci`` source reference may
connect remediation workflow signal to an admitted MSP-B3 graph entity.  No
display-name, host-label, description, assignment, or similarity matching is
performed.
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping

from app.incident_ci_resolution import (
    observed_ci_dependency_hops,
    resolve_explicit_servicenow_ci_reference,
)
from database.models.entities import Entity
from discovery.signals.remediation_signature import apply_remediation_signatures


RESOLUTION_VERSION = "servicenow_explicit_vulnerable_item_ci_v1"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _metadata(entity: Entity) -> dict[str, Any]:
    return entity.metadata if isinstance(entity.metadata, dict) else {}


def _base_resolution(item: Mapping[str, Any], org_id: str) -> dict[str, Any]:
    item_sys_id = _text(item.get("sys_id")) or None
    return {
        "version": RESOLUTION_VERSION,
        "status": "unresolved",
        "reason": "missing_explicit_reference",
        "method": "vulnerable_item_cmdb_ci",
        "reference_field": "cmdb_ci",
        "org_id": org_id,
        "source_system": "servicenow",
        "source_type": item.get("source_type"),
        "source_artifact": item_sys_id,
        "source_record_id": item_sys_id,
        "vulnerable_item_sys_id": item_sys_id,
        "vulnerable_item_number": _text(item.get("number")) or None,
        "ci_sys_id": None,
        "ci_entity_id": None,
        "source_timestamp": item.get("source_timestamp"),
        "source_url": item.get("source_url"),
    }


def _set_unresolved(
    item: dict[str, Any],
    resolution: dict[str, Any],
    reason: str,
    *,
    ci_sys_id: str | None = None,
) -> None:
    resolution.update(
        {
            "status": "unresolved",
            "reason": reason,
            "ci_sys_id": ci_sys_id,
            "ci_entity_id": None,
        }
    )
    item["ci_resolution"] = resolution
    # A later source or scope change must not leave a stale successful join.
    item.pop("ci_sys_id", None)
    item.pop("ci_entity_id", None)
    item.pop("resolved_ci", None)
    item.pop("ci_evidence_trace", None)


def resolve_vulnerable_item_ci_references(
    *,
    org_id: str,
    vulnerable_item_metrics: dict[str, Any],
    cmdb_entities: Iterable[Entity],
    cmdb_relationships: Iterable[Mapping[str, Any]] = (),
) -> dict[str, int]:
    """Annotate vulnerable-item workflow signals with exact CI graph joins.

    Resolution is deterministic and organization-scoped.  Missing, malformed,
    ambiguous, retired, and out-of-scope references remain unresolved with a
    reason.  A successful result includes a provenance-rich item-to-CI hop and
    all explicit outgoing CMDB dependency hops supplied by the caller.
    """
    entities = list(cmdb_entities)
    relationships = list(cmdb_relationships)
    counts = {"resolved": 0, "unresolved": 0}
    payload_org = _text(vulnerable_item_metrics.get("org_id"))

    for item in vulnerable_item_metrics.get("vulnerable_items", []) or []:
        if not isinstance(item, dict):
            continue
        resolution = _base_resolution(item, org_id)
        item_sys_id = _text(item.get("sys_id"))
        item_org = _text(item.get("org_id"))

        if not item_sys_id:
            _set_unresolved(item, resolution, "missing_vulnerable_item_identifier")
            counts["unresolved"] += 1
            continue
        if (payload_org and payload_org != org_id) or (item_org and item_org != org_id):
            _set_unresolved(item, resolution, "organization_mismatch")
            counts["unresolved"] += 1
            continue

        ci_result = resolve_explicit_servicenow_ci_reference(
            org_id=org_id,
            reference=item.get("cmdb_ci"),
            entities=entities,
        )
        if ci_result.reason is not None or ci_result.entity is None:
            _set_unresolved(
                item,
                resolution,
                ci_result.reason or "ambiguous_ci_entity",
                ci_sys_id=ci_result.ci_sys_id,
            )
            counts["unresolved"] += 1
            continue

        entity = ci_result.entity
        ci_sys_id = ci_result.ci_sys_id or ""
        metadata = _metadata(entity)
        resolution.update(
            {
                "status": "resolved",
                "reason": None,
                "ci_sys_id": ci_sys_id,
                "ci_entity_id": str(entity.id),
            }
        )

        item_to_ci = {
            "origin": "observed",
            "relationship_type": "references",
            "reference_field": "cmdb_ci",
            "org_id": org_id,
            "source_system": "servicenow",
            "source_type": item.get("source_type"),
            "source_artifact": item_sys_id,
            "source_record_id": item_sys_id,
            "originating_servicenow_record": item_sys_id,
            "source_timestamp": item.get("source_timestamp"),
            "source_url": item.get("source_url"),
            "vulnerable_item_sys_id": item_sys_id,
            "vulnerable_item_source_url": item.get("source_url"),
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
            hop["org_id"] = org_id
            hop["originating_servicenow_record"] = hop.get("source_record_id")
            hop["from_ci_entity_id"] = str(entity.id)

        item["ci_resolution"] = resolution
        item["ci_sys_id"] = ci_sys_id
        item["ci_entity_id"] = str(entity.id)
        item["resolved_ci"] = {
            "entity_id": str(entity.id),
            "org_id": org_id,
            "source_system": "servicenow",
            "source_record_id": ci_sys_id,
            "display_name": entity.display_name,
            "ci_class": metadata.get("ci_class"),
            "operational_status": metadata.get("operational_status"),
            "lifecycle_state": metadata.get("lifecycle_state"),
            "source_timestamp": metadata.get("source_updated_at"),
            "source_url": metadata.get("source_url"),
            "evidence_pointer": metadata.get("evidence_pointer"),
        }
        item["ci_evidence_trace"] = {
            "vulnerable_item_to_ci": item_to_ci,
            "ci_dependencies": dependencies,
        }
        counts["resolved"] += 1

    vulnerable_item_metrics["ci_resolution_summary"] = counts.copy()
    vulnerable_item_metrics["remediation_workload_summary"] = (
        apply_remediation_signatures(
            vulnerable_item_metrics.get("vulnerable_items", []) or []
        )
    )
    return counts
