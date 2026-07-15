"""MSP-B3 T4 contract: explicit ServiceNow incident-to-CI resolution."""
from __future__ import annotations

from uuid import uuid4

from app import db
from app.entity_extractor import _extract_servicenow_cmdb_entities
from app.incident_ci_resolution import resolve_incident_ci_references


def _item(sys_id: str, name: str, status: str = "Operational") -> dict:
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


def _entities(org_id: str, run_id: str, *items: dict):
    return _extract_servicenow_cmdb_entities(
        org_id=org_id,
        run_id=run_id,
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


def test_primary_and_secondary_references_resolve_with_auditable_two_hop_trace():
    org_id = f"org-ci-resolution-{uuid4().hex}"
    try:
        entities = _entities(
            org_id,
            "run-ci-resolution",
            _item("ci-app", "Citizen Portal"),
            _item("ci-host", "app-prod-01"),
        )
        relationships = [_relationship("ci-app", "ci-host")]
        incident_metrics = {
            "affected_ci_lookup": {"status": "available"},
            "incidents": [
                {
                    "sys_id": "incident-primary",
                    "number": "INC001",
                    "cmdb_ci": {
                        "value": "ci-app",
                        "display_value": "Citizen Portal",
                    },
                    "description": "This free text is never used for resolution.",
                    "source_timestamp": "2026-07-14 11:00:00",
                    "source_url": "https://acme.service-now.com/incident-primary",
                },
                {
                    "sys_id": "incident-secondary",
                    "number": "INC002",
                    "cmdb_ci": None,
                    "affected_ci_references": [
                        {
                            "relationship_sys_id": "task-ci-1",
                            "incident_sys_id": "incident-secondary",
                            "ci_sys_id": "ci-app",
                            "source_type": "servicenow_task_ci",
                            "source_timestamp": "2026-07-14 11:05:00",
                            "source_url": "https://acme.service-now.com/task-ci-1",
                            "origin": "observed",
                        }
                    ],
                    "source_url": "https://acme.service-now.com/incident-secondary",
                },
            ],
        }

        counts = resolve_incident_ci_references(
            org_id=org_id,
            incident_metrics=incident_metrics,
            cmdb_entities=entities,
            cmdb_relationships=relationships,
        )

        assert counts == {"resolved": 2, "unresolved": 0}
        primary, secondary = incident_metrics["incidents"]
        app_entity = next(e for e in entities if e.source_record_id == "ci-app")
        host_entity = next(e for e in entities if e.source_record_id == "ci-host")
        assert primary["ci_entity_id"] == str(app_entity.id)
        assert primary["ci_resolution"]["method"] == "incident_cmdb_ci"
        assert primary["ci_resolution"]["reference_field"] == "cmdb_ci"
        assert primary["ci_resolution"]["incident_sys_id"] == "incident-primary"
        assert primary["ci_resolution"]["ci_sys_id"] == "ci-app"
        assert secondary["ci_entity_id"] == str(app_entity.id)
        assert secondary["ci_resolution"]["method"] == "affected_ci_task"
        assert secondary["ci_resolution"]["supporting_relationship_ids"] == [
            "task-ci-1"
        ]

        first_hop = primary["ci_evidence_trace"]["incident_to_ci"]
        assert first_hop["origin"] == "observed"
        assert first_hop["source_artifact"] == "incident-primary"
        assert first_hop["incident_sys_id"] == "incident-primary"
        assert first_hop["ci_sys_id"] == "ci-app"
        dependency_hop = primary["ci_evidence_trace"]["ci_dependencies"][0]
        assert dependency_hop == {
            "origin": "observed",
            "relationship_type": "runs_on",
            "source_system": "servicenow",
            "source_artifact": "rel-app-host",
            "source_record_id": "rel-app-host",
            "source_type": "servicenow_cmdb_rel_ci",
            "source_timestamp": "2026-07-14 10:05:00",
            "source_url": (
                "https://acme.service-now.com/nav_to.do?"
                "uri=cmdb_rel_ci.do%3Fsys_id%3Drel-app-host"
            ),
            "from_ci_sys_id": "ci-app",
            "from_ci_entity_id": str(app_entity.id),
            "to_ci_sys_id": "ci-host",
            "to_ci_entity_id": str(host_entity.id),
        }
        secondary_hop = secondary["ci_evidence_trace"]["incident_to_ci"]
        assert secondary_hop["source_artifact"] == "task-ci-1"
        assert secondary_hop["source_url"].endswith("task-ci-1")
    finally:
        _cleanup(org_id)


def test_unsafe_references_remain_unresolved_without_name_or_cross_org_matching():
    org_a = f"org-ci-resolution-a-{uuid4().hex}"
    org_b = f"org-ci-resolution-b-{uuid4().hex}"
    try:
        org_a_entities = _entities(
            org_a,
            "run-a",
            _item("ci-active", "Shared Name"),
            _item("ci-retired", "Retired System", "Retired"),
        )
        # Same display name and a source ID referenced by org A, but owned by B.
        org_b_entities = _entities(
            org_b,
            "run-b",
            _item("ci-other-org", "Shared Name"),
        )
        incidents = [
            {
                "sys_id": "incident-free-text",
                "number": "INC010",
                "description": "Shared Name is unavailable",
            },
            {
                "sys_id": "incident-display-only",
                "number": "INC011",
                "cmdb_ci": {"display_value": "Shared Name"},
            },
            {
                "sys_id": "incident-other-org",
                "number": "INC012",
                "cmdb_ci": "ci-other-org",
            },
            {
                "sys_id": "incident-retired",
                "number": "INC013",
                "cmdb_ci": "ci-retired",
            },
            {
                "sys_id": "incident-ambiguous",
                "number": "INC014",
                "affected_ci_references": [
                    {
                        "relationship_sys_id": "task-ci-a",
                        "incident_sys_id": "incident-ambiguous",
                        "ci_sys_id": "ci-active",
                    },
                    {
                        "relationship_sys_id": "task-ci-b",
                        "incident_sys_id": "incident-ambiguous",
                        "ci_sys_id": "ci-retired",
                    },
                ],
            },
        ]
        metrics = {
            "affected_ci_lookup": {"status": "available"},
            "incidents": incidents,
        }

        counts = resolve_incident_ci_references(
            org_id=org_a,
            incident_metrics=metrics,
            cmdb_entities=org_a_entities + org_b_entities,
        )

        assert counts == {"resolved": 0, "unresolved": 5}
        assert [i["ci_resolution"]["reason"] for i in incidents] == [
            "missing_explicit_reference",
            "invalid_primary_reference",
            "ci_not_in_current_scope",
            "retired_ci",
            "ambiguous_affected_ci_reference",
        ]
        assert incidents[0]["description"] == "Shared Name is unavailable"
        assert all("ci_entity_id" not in incident for incident in incidents)
        assert incidents[4]["ci_resolution"]["candidate_ci_sys_ids"] == [
            "ci-active",
            "ci-retired",
        ]
    finally:
        _cleanup(org_a, org_b)
