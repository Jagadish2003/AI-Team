"""MSP-B11 T3: explicit vulnerable-item-to-CMDB entity contract."""
from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

from app import db
from app.entity_extractor import (
    _extract_servicenow_cmdb_entities,
    prepare_servicenow_ci_resolution,
)
from app.vulnerable_item_ci_resolution import (
    resolve_vulnerable_item_ci_references,
)
from app.relationship_mapper import apply_servicenow_cmdb_relationship_delta


def _ci(sys_id: str, name: str, status: str = "Operational") -> dict:
    return {
        "sys_id": sys_id,
        "name": name,
        "ci_class": "cmdb_ci_server",
        "operational_status": status,
        "assignment_group": "Platform Operations",
        "owned_by": "Infrastructure",
        "environment": "production",
        "updated_at": "2026-07-14 10:00:00",
        "source_url": (
            "https://acme.service-now.com/nav_to.do?"
            f"uri=cmdb_ci.do%3Fsys_id%3D{sys_id}"
        ),
    }


def _relationship(source: str, target: str) -> dict:
    return {
        "sys_id": "rel-app-host",
        "relationship_type": "runs_on",
        "source_ci_id": source,
        "target_ci_id": target,
        "source_type": "servicenow_cmdb_rel_ci",
        "source_timestamp": "2026-07-14 10:05:00",
        "source_url": (
            "https://acme.service-now.com/nav_to.do?"
            "uri=cmdb_rel_ci.do%3Fsys_id%3Drel-app-host"
        ),
        "origin": "observed",
    }


def _vulnerable_item(
    sys_id: str,
    org_id: str,
    cmdb_ci: object = "ci-app",
) -> dict:
    return {
        "sys_id": sys_id,
        "number": sys_id.upper(),
        "org_id": org_id,
        "cmdb_ci": cmdb_ci,
        "description": "Names and free text are never used for resolution.",
        "source_type": "servicenow_vulnerable_item",
        "source_timestamp": "2026-07-14 11:00:00",
        "source_url": f"https://acme.service-now.com/{sys_id}",
        "origin": "observed",
    }


def _entities(org_id: str, *items: dict):
    return _extract_servicenow_cmdb_entities(
        org_id=org_id,
        run_id=f"run-{org_id}",
        cmdb_data={
            "org_id": org_id,
            "class_scope": ["cmdb_ci_server"],
            "configuration_items": list(items),
        },
    )


def _cleanup(*org_ids: str) -> None:
    conn = db.connect()
    try:
        cur = conn.cursor()
        for org_id in org_ids:
            cur.execute("DELETE FROM entity_relationships WHERE org_id = %s", (org_id,))
            cur.execute("DELETE FROM entities WHERE org_id = %s", (org_id,))
        conn.commit()
    finally:
        conn.close()


def test_explicit_reference_is_resolved_before_detectors_with_provenance_on_each_hop():
    org_id = f"org-vr-ci-{uuid4().hex}"
    try:
        item = _vulnerable_item("vi-001", org_id)
        relationship = _relationship("ci-app", "ci-host")
        sn_data = {
            "cmdb": {
                "org_id": org_id,
                "class_scope": ["cmdb_ci_server"],
                "configuration_items": [
                    _ci("ci-app", "Citizen Portal"),
                    _ci("ci-host", "app-prod-01"),
                ],
                "relationships": [relationship],
            },
            "incident_metrics": {"incidents": []},
            "vulnerability_response": {
                "org_id": org_id,
                "vulnerable_items": [item],
            },
        }

        entities = prepare_servicenow_ci_resolution(
            org_id=org_id,
            run_id="run-vr-ci",
            sn_data=sn_data,
        )

        app = next(entity for entity in entities if entity.source_record_id == "ci-app")
        host = next(entity for entity in entities if entity.source_record_id == "ci-host")
        assert item["ci_entity_id"] == str(app.id)
        assert item["ci_resolution"] == {
            "version": "servicenow_explicit_vulnerable_item_ci_v1",
            "status": "resolved",
            "reason": None,
            "method": "vulnerable_item_cmdb_ci",
            "reference_field": "cmdb_ci",
            "org_id": org_id,
            "source_system": "servicenow",
            "source_type": "servicenow_vulnerable_item",
            "source_artifact": "vi-001",
            "source_record_id": "vi-001",
            "vulnerable_item_sys_id": "vi-001",
            "vulnerable_item_number": "VI-001",
            "ci_sys_id": "ci-app",
            "ci_entity_id": str(app.id),
            "source_timestamp": "2026-07-14 11:00:00",
            "source_url": "https://acme.service-now.com/vi-001",
        }
        assert item["resolved_ci"]["org_id"] == org_id
        assert item["resolved_ci"]["source_record_id"] == "ci-app"
        ci_pointer = item["resolved_ci"]["evidence_pointer"]
        assert ci_pointer["origin"] == "observed"
        assert ci_pointer["source_system"] == "servicenow"
        assert ci_pointer["source_artifact"] == "ci-app"
        assert ci_pointer["source_timestamp"] == "2026-07-14 10:00:00"
        assert ci_pointer["extraction_job_id"] is None
        assert ci_pointer["confidence"] == 1.0

        item_hop = item["ci_evidence_trace"]["vulnerable_item_to_ci"]
        assert item_hop["origin"] == "observed"
        assert item_hop["relationship_type"] == "references"
        assert item_hop["org_id"] == org_id
        assert item_hop["source_system"] == "servicenow"
        assert item_hop["source_artifact"] == "vi-001"
        assert item_hop["source_timestamp"] == "2026-07-14 11:00:00"
        assert item_hop["originating_servicenow_record"] == "vi-001"
        assert item_hop["ci_entity_id"] == str(app.id)

        dependency_hop = item["ci_evidence_trace"]["ci_dependencies"][0]
        assert dependency_hop["origin"] == "observed"
        assert dependency_hop["relationship_type"] == "runs_on"
        assert dependency_hop["org_id"] == org_id
        assert dependency_hop["source_system"] == "servicenow"
        assert dependency_hop["source_artifact"] == "rel-app-host"
        assert dependency_hop["source_timestamp"] == "2026-07-14 10:05:00"
        assert dependency_hop["originating_servicenow_record"] == "rel-app-host"
        assert dependency_hop["from_ci_entity_id"] == str(app.id)
        assert dependency_hop["to_ci_entity_id"] == str(host.id)
        assert sn_data["vulnerability_response"]["ci_resolution_summary"] == {
            "resolved": 1,
            "unresolved": 0,
        }
    finally:
        _cleanup(org_id)


def test_missing_malformed_ambiguous_retired_and_out_of_scope_refs_stay_unresolved():
    org_a = f"org-vr-a-{uuid4().hex}"
    org_b = f"org-vr-b-{uuid4().hex}"
    try:
        org_a_entities = _entities(
            org_a,
            _ci("ci-active", "Shared Name"),
            _ci("ci-ambiguous", "Duplicate Candidate"),
            _ci("ci-retired", "Retired Server", "Retired"),
        )
        org_b_entities = _entities(
            org_b,
            _ci("ci-other-org", "Shared Name"),
        )
        ambiguous = next(
            entity for entity in org_a_entities
            if entity.source_record_id == "ci-ambiguous"
        )
        duplicate_candidate = replace(ambiguous, id=uuid4())
        items = [
            _vulnerable_item("vi-missing", org_a, None),
            _vulnerable_item(
                "vi-malformed",
                org_a,
                {"display_value": "Shared Name"},
            ),
            _vulnerable_item("vi-ambiguous", org_a, "ci-ambiguous"),
            _vulnerable_item("vi-retired", org_a, "ci-retired"),
            _vulnerable_item("vi-out-of-scope", org_a, "ci-other-org"),
            _vulnerable_item("vi-wrong-org", org_b, "ci-active"),
        ]
        metrics = {"org_id": org_a, "vulnerable_items": items}

        counts = resolve_vulnerable_item_ci_references(
            org_id=org_a,
            vulnerable_item_metrics=metrics,
            cmdb_entities=(
                org_a_entities
                + [duplicate_candidate]
                + org_b_entities
            ),
        )

        assert counts == {"resolved": 0, "unresolved": 6}
        assert [item["ci_resolution"]["reason"] for item in items] == [
            "missing_explicit_reference",
            "invalid_explicit_reference",
            "ambiguous_ci_entity",
            "retired_ci",
            "ci_not_in_current_scope",
            "organization_mismatch",
        ]
        assert all("ci_entity_id" not in item for item in items)
        assert items[1]["description"].startswith("Names and free text")
    finally:
        _cleanup(org_a, org_b)


def test_incremental_vr_delta_traverses_an_unchanged_active_cmdb_relationship():
    org_id = f"org-vr-existing-edge-{uuid4().hex}"
    try:
        entities = _entities(
            org_id,
            _ci("ci-app", "Citizen Portal"),
            _ci("ci-host", "app-prod-01"),
        )
        assert apply_servicenow_cmdb_relationship_delta(
            org_id=org_id,
            run_id="run-cmdb-initial",
            relationships=[_relationship("ci-app", "ci-host")],
            entities=entities,
        ) == 1
        item = _vulnerable_item("vi-later-delta", org_id)
        sn_data = {
            "cmdb": {
                "org_id": org_id,
                "class_scope": ["cmdb_ci_server"],
                "configuration_items": [],
                "relationships": [],
            },
            "vulnerability_response": {
                "org_id": org_id,
                "vulnerable_items": [item],
            },
        }

        prepare_servicenow_ci_resolution(
            org_id=org_id,
            run_id="run-vr-later",
            sn_data=sn_data,
        )

        dependency = item["ci_evidence_trace"]["ci_dependencies"][0]
        assert dependency["source_artifact"] == "rel-app-host"
        assert dependency["relationship_type"] == "runs_on"
        assert dependency["origin"] == "observed"
        assert dependency["org_id"] == org_id
    finally:
        _cleanup(org_id)


def test_identical_ci_source_ids_resolve_to_the_current_organization_only():
    org_a = f"org-vr-shared-a-{uuid4().hex}"
    org_b = f"org-vr-shared-b-{uuid4().hex}"
    try:
        entity_a = _entities(org_a, _ci("ci-shared", "Alpha Service"))[0]
        entity_b = _entities(org_b, _ci("ci-shared", "Bravo Service"))[0]
        all_entities = [entity_a, entity_b]
        item_a = _vulnerable_item("vi-a", org_a, "ci-shared")
        item_b = _vulnerable_item("vi-b", org_b, "ci-shared")

        resolve_vulnerable_item_ci_references(
            org_id=org_a,
            vulnerable_item_metrics={"org_id": org_a, "vulnerable_items": [item_a]},
            cmdb_entities=all_entities,
        )
        resolve_vulnerable_item_ci_references(
            org_id=org_b,
            vulnerable_item_metrics={"org_id": org_b, "vulnerable_items": [item_b]},
            cmdb_entities=all_entities,
        )

        assert item_a["ci_entity_id"] == str(entity_a.id)
        assert item_b["ci_entity_id"] == str(entity_b.id)
        assert item_a["ci_entity_id"] != item_b["ci_entity_id"]
        assert item_a["resolved_ci"]["display_name"] == "Alpha Service"
        assert item_b["resolved_ci"]["display_name"] == "Bravo Service"
    finally:
        _cleanup(org_a, org_b)
