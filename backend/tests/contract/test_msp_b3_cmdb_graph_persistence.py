"""MSP-B3 T3: ServiceNow CMDB entities and observed graph relationships."""
from __future__ import annotations

import json
from uuid import uuid4

import pytest

from app import db
from app.entity_extractor import _extract_servicenow_cmdb_entities, extract_entities
from app.entity_resolution import get_source_entity
from app.relationship_mapper import map_relationships


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


def _relationship(parent: str, child: str) -> dict:
    return {
        "sys_id": "rel-001",
        "relationship_type": "runs_on",
        "source_ci_id": parent,
        "target_ci_id": child,
        "servicenow_parent_id": parent,
        "servicenow_child_id": child,
        "source_relationship_name": "Runs on::Runs",
        "source_type": "servicenow_cmdb_rel_ci",
        "source_timestamp": "2026-07-14 10:05:00",
        "source_url": (
            "https://acme.service-now.com/nav_to.do?"
            "uri=cmdb_rel_ci.do%3Fsys_id%3Drel-001"
        ),
        "origin": "observed",
    }


def _payload(org_id: str, app_name: str = "Citizen Portal", status: str = "Operational"):
    return {
        "org_id": org_id,
        "class_scope": ["cmdb_ci_server"],
        "configuration_items": [
            _item("ci-app", app_name, status),
            _item("ci-host", "app-prod-01"),
            {
                **_item("ci-printer", "Office Printer"),
                "ci_class": "cmdb_ci_printer",
            },
        ],
        "relationships": [_relationship("ci-app", "ci-host")],
    }


def _persist(org_id: str, run_id: str, payload: dict):
    ingestor_data = {"servicenow": {"cmdb": payload}}
    entities = extract_entities(
        org_id=org_id,
        run_id=run_id,
        pack_id="msp_readiness",
        detector_results=[],
        ingestor_data=ingestor_data,
    )
    counts = map_relationships(
        org_id=org_id,
        run_id=run_id,
        ingestor_data=ingestor_data,
        detector_results=[],
        entities=entities,
    )
    return entities, counts


def _rows(sql: str, params: tuple):
    conn = db.connect()
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()


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


def test_cmdb_graph_rejects_a_payload_from_another_org_before_writing():
    with pytest.raises(ValueError, match="does not match"):
        _extract_servicenow_cmdb_entities(
            org_id="org-expected",
            run_id="run-mismatch",
            cmdb_data=_payload("org-other"),
        )


def test_cmdb_graph_persistence_is_observed_idempotent_and_org_scoped():
    org_a = f"org-cmdb-a-{uuid4().hex}"
    org_b = f"org-cmdb-b-{uuid4().hex}"
    try:
        entities, counts = _persist(org_a, "run-cmdb-a-1", _payload(org_a))
        assert counts["observed"] == 1
        assert {entity.source_record_id for entity in entities} == {"ci-app", "ci-host"}

        entity_rows = _rows(
            "SELECT * FROM entities WHERE org_id = %s ORDER BY source_record_id",
            (org_a,),
        )
        assert len(entity_rows) == 2
        app_row = next(row for row in entity_rows if row["source_record_id"] == "ci-app")
        app_id = app_row["id"]
        app_metadata = json.loads(app_row["metadata"])
        assert app_row["entity_type"] == "system"
        assert app_row["source_system"] == "servicenow"
        assert app_row["resolution_status"] == "resolved"
        assert app_metadata["ci_class"] == "cmdb_ci_server"
        assert app_metadata["operational_status"] == "Operational"
        assert app_metadata["lifecycle_state"] == "active"
        assert app_metadata["owner"] == "Infrastructure"
        assert app_metadata["environment"] == "production"
        assert app_metadata["source_url"].endswith("sys_id%3Dci-app")
        assert app_metadata["evidence_pointer"]["origin"] == "observed"
        assert app_metadata["evidence_pointer"]["source_system"] == "servicenow"
        assert app_metadata["evidence_pointer"]["source_artifact"] == "ci-app"
        assert app_metadata["evidence_pointer"]["source_timestamp"] == (
            "2026-07-14 10:00:00"
        )

        relationship_rows = _rows(
            "SELECT * FROM entity_relationships WHERE org_id = %s",
            (org_a,),
        )
        assert len(relationship_rows) == 1
        relationship_id = relationship_rows[0]["id"]
        evidence = json.loads(relationship_rows[0]["evidence"])
        assert relationship_rows[0]["relationship_type"] == "runs_on"
        assert relationship_rows[0]["inferred"] is False
        assert evidence["relationship_sys_id"] == "rel-001"
        assert evidence["source_timestamp"] == "2026-07-14 10:05:00"
        assert evidence["source_url"].endswith("sys_id%3Drel-001")
        assert evidence["evidence_pointer"]["origin"] == "observed"
        assert evidence["evidence_pointer"]["source_artifact"] == "rel-001"

        # Same source data in the same run confirms, but does not duplicate/count.
        _persist(org_a, "run-cmdb-a-1", _payload(org_a))
        assert len(_rows("SELECT * FROM entities WHERE org_id = %s", (org_a,))) == 2
        same_edge = _rows(
            "SELECT * FROM entity_relationships WHERE org_id = %s", (org_a,)
        )[0]
        assert same_edge["id"] == relationship_id
        assert same_edge["run_count"] == 1

        # A later source observation updates the same stable entity and lifecycle.
        _persist(
            org_a,
            "run-cmdb-a-2",
            _payload(org_a, app_name="Citizen Portal v2", status="Retired"),
        )
        updated_app = _rows(
            "SELECT * FROM entities WHERE org_id = %s AND source_record_id = %s",
            (org_a, "ci-app"),
        )[0]
        assert updated_app["id"] == app_id
        assert updated_app["display_name"] == "Citizen Portal v2"
        assert updated_app["run_count"] == 2
        updated_metadata = json.loads(updated_app["metadata"])
        assert updated_metadata["lifecycle_state"] == "retired"
        assert updated_metadata["is_retired"] is True
        updated_edge = _rows(
            "SELECT * FROM entity_relationships WHERE org_id = %s", (org_a,)
        )[0]
        assert updated_edge["id"] == relationship_id
        assert updated_edge["run_count"] == 2

        # The same ServiceNow sys_ids in another tenant create isolated graph rows.
        _persist(org_b, "run-cmdb-b-1", _payload(org_b))
        other_app = get_source_entity(
            org_id=org_b,
            entity_type="system",
            source_system="servicenow",
            source_record_id="ci-app",
        )
        assert other_app is not None
        assert str(other_app.id) != app_id
        assert len(_rows("SELECT * FROM entities WHERE org_id = %s", (org_b,))) == 2
        assert len(
            _rows("SELECT * FROM entity_relationships WHERE org_id = %s", (org_b,))
        ) == 1

        # Stable source lookup is the exact incident-to-CI join point for T4;
        # the returned node already participates in the persisted dependency edge.
        resolved_ci = get_source_entity(
            org_id=org_a,
            entity_type="system",
            source_system="servicenow",
            source_record_id="ci-app",
        )
        assert resolved_ci is not None
        dependency = _rows(
            "SELECT er.relationship_type, target.source_record_id "
            "FROM entity_relationships er "
            "JOIN entities target ON target.id = er.to_entity_id "
            "WHERE er.org_id = %s AND er.from_entity_id = %s",
            (org_a, str(resolved_ci.id)),
        )
        assert dependency == [
            {"relationship_type": "runs_on", "source_record_id": "ci-host"}
        ]
    finally:
        _cleanup(org_a, org_b)
