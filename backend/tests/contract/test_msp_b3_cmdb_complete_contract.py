"""Focused end-to-end contract for MSP-B3 ServiceNow CMDB ingestion.

The suite deliberately uses deterministic ServiceNow Table API responses while
driving the real organization configuration, checkpoint repository, entity
resolution, relationship persistence, provenance, and incident-resolution code.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit
from uuid import uuid4

from app import db
from app.entity_resolution import get_source_entity, list_source_entities
from app.incident_ci_resolution import resolve_incident_ci_references
from discovery.ingest import checkpoint_repository
from discovery.ingest import servicenow as sn


INITIAL_WATERMARK = datetime(2026, 7, 15, 10, 0, tzinfo=timezone.utc)
DELTA_WATERMARK = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)


def _ci(
    sys_id: str,
    name: str,
    ci_class: str,
    *,
    updated_at: str = "2026-07-15 09:00:00",
    status: str = "Operational",
    assignment_group: str = "Platform Operations",
    owned_by: str = "Infrastructure",
    environment: str = "production",
) -> dict:
    return {
        "sys_id": sys_id,
        "name": name,
        "sys_class_name": ci_class,
        "operational_status": status,
        "assignment_group": assignment_group,
        "owned_by": owned_by,
        "environment": environment,
        "sys_updated_on": updated_at,
        # These must never cross the bounded projection.
        "discovery_credentials": "forbidden",
        "ip_address": "192.0.2.1",
    }


def _relationship(
    sys_id: str,
    parent: str,
    child: str,
    label: str,
    *,
    parent_class: str,
    child_class: str,
    updated_at: str = "2026-07-15 09:05:00",
    active: str = "true",
) -> dict:
    return {
        "sys_id": sys_id,
        "parent": {"value": parent, "display_value": parent},
        "child": {"value": child, "display_value": child},
        "type": {"value": f"type-{sys_id}", "display_value": label},
        "active": active,
        "sys_updated_on": updated_at,
        "_parent_class": parent_class,
        "_child_class": child_class,
    }


class DeterministicReadOnlyServiceNow:
    """Read-only Table API double with server-side query semantics."""

    instance_url = "https://acme.service-now.com"

    def __init__(self, cis: list[dict], relationships: list[dict]) -> None:
        self.cis = cis
        self.relationships = relationships
        self.relationship_deletions: list[dict] = []
        self.leaked_relationship_ids: set[str] = set()
        self.read_calls: list[dict] = []
        self.write_attempts: list[str] = []

    @staticmethod
    def _cursor_window(records: list[dict], query: str) -> list[dict]:
        after = re.search(r"sys_updated_on>([^\^]+)", query)
        through = re.search(r"sys_updated_on<=([^\^]+)", query)
        result = []
        for record in records:
            timestamp = str(record.get("sys_updated_on") or "")
            if after and timestamp <= after.group(1):
                continue
            if through and timestamp > through.group(1):
                continue
            result.append(record)
        return result

    @staticmethod
    def _class_values(query: str, field: str) -> set[str]:
        match = re.search(rf"{re.escape(field)}IN([^\^]+)", query)
        return set(match.group(1).split(",")) if match else set()

    @staticmethod
    def _public(record: dict) -> dict:
        return {key: value for key, value in record.items() if not key.startswith("_")}

    def table_query(self, table: str, params: dict, max_records: int) -> list[dict]:
        query = params.get("sysparm_query", "")
        self.read_calls.append(
            {
                "operation": "table_query",
                "table": table,
                "query": query,
                "fields": params.get("sysparm_fields"),
                "max_records": max_records,
            }
        )
        if table == "cmdb_ci":
            allowed = self._class_values(query, "sys_class_name")
            records = [r for r in self.cis if r["sys_class_name"] in allowed]
            return [self._public(r) for r in self._cursor_window(records, query)]
        if table == "cmdb_rel_ci":
            parent_scope = self._class_values(query, "parent.sys_class_name")
            child_scope = self._class_values(query, "child.sys_class_name")
            records = [
                r
                for r in self.relationships
                if (
                    r["_parent_class"] in parent_scope
                    and r["_child_class"] in child_scope
                )
                or r["sys_id"] in self.leaked_relationship_ids
            ]
            return [self._public(r) for r in self._cursor_window(records, query)]
        if table == "sys_audit_delete":
            known_match = re.search(r"documentkeyIN([^\^]+)", query)
            known = set(known_match.group(1).split(",")) if known_match else set()
            records = [
                r for r in self.relationship_deletions
                if r.get("documentkey") in known
            ]
            return [self._public(r) for r in self._cursor_window(records, query)]
        raise AssertionError(f"unexpected ServiceNow table read: {table}")

    def _forbid(self, operation: str) -> None:
        self.write_attempts.append(operation)
        raise AssertionError(f"CMDB write attempted: {operation}")

    def create_record(self, *args, **kwargs):
        self._forbid("create")

    def update_record(self, *args, **kwargs):
        self._forbid("update")

    def delete_record(self, *args, **kwargs):
        self._forbid("delete")

    def remediate(self, *args, **kwargs):
        self._forbid("remediate")

    def reconcile(self, *args, **kwargs):
        self._forbid("reconcile")

    def run_data_quality(self, *args, **kwargs):
        self._forbid("data_quality")


def _base_client() -> DeterministicReadOnlyServiceNow:
    client = DeterministicReadOnlyServiceNow(
        cis=[
            _ci("ci-app", "Citizen Portal", "cmdb_ci_server"),
            _ci("ci-db", "Citizen Database", "cmdb_ci_db_instance"),
            _ci("ci-host", "app-prod-01", "cmdb_ci_server"),
            _ci("ci-printer", "Office Printer", "cmdb_ci_printer"),
        ],
        relationships=[
            _relationship(
                "rel-depends", "ci-app", "ci-db", "Depends on::Used by",
                parent_class="cmdb_ci_server",
                child_class="cmdb_ci_db_instance",
            ),
            _relationship(
                "rel-used", "ci-app", "ci-host", "Used by::Uses",
                parent_class="cmdb_ci_server",
                child_class="cmdb_ci_server",
            ),
            _relationship(
                "rel-runs", "ci-app", "ci-host", "Runs on::Runs",
                parent_class="cmdb_ci_server",
                child_class="cmdb_ci_server",
            ),
            _relationship(
                "rel-connects", "ci-db", "ci-host", "Connects to::Connected by",
                parent_class="cmdb_ci_db_instance",
                child_class="cmdb_ci_server",
            ),
            _relationship(
                "rel-outside", "ci-app", "ci-printer", "Depends on::Used by",
                parent_class="cmdb_ci_server",
                child_class="cmdb_ci_printer",
            ),
        ],
    )
    # Simulate an upstream response race to prove the exact endpoint-ID barrier
    # still rejects an out-of-scope relationship after the server-side filter.
    client.leaked_relationship_ids.add("rel-outside")
    return client


def _configure(org_id: str, classes: list[str]) -> None:
    db.org_connector_set(
        org_id,
        "servicenow",
        {
            "id": "servicenow",
            "name": "ServiceNow",
            "status": "connected",
            sn.CMDB_CLASS_SCOPE_CONFIG_KEY: classes,
        },
    )


def _rows(sql: str, params: tuple) -> list[dict]:
    conn = db.connect()
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()


def _json(value):
    return json.loads(value) if isinstance(value, str) else value


def _cleanup(*org_ids: str) -> None:
    conn = db.connect()
    try:
        cur = conn.cursor()
        for org_id in org_ids:
            cur.execute("DELETE FROM entity_relationships WHERE org_id = %s", (org_id,))
            cur.execute("DELETE FROM entities WHERE org_id = %s", (org_id,))
            cur.execute("DELETE FROM ingestion_checkpoints WHERE org_id = %s", (org_id,))
            cur.execute(
                "DELETE FROM connectors WHERE id = %s",
                (f"{org_id}::servicenow",),
            )
        conn.commit()
    finally:
        conn.close()


def _initial_load(org_id: str, client: DeterministicReadOnlyServiceNow) -> dict:
    return sn.ingest_cmdb_changes(
        org_id=org_id,
        run_id=f"run-{org_id}-initial",
        client=client,
        clock=lambda: INITIAL_WATERMARK,
    )


def test_bounded_graph_provenance_normalized_edges_and_two_org_isolation(monkeypatch):
    org_a = f"msp-b3-complete-a-{uuid4().hex}"
    org_b = f"msp-b3-complete-b-{uuid4().hex}"
    client_a = _base_client()
    client_b = DeterministicReadOnlyServiceNow(
        cis=[
            _ci("ci-app", "Tenant B Load Balancer", "cmdb_ci_lb"),
            _ci("ci-lb-2", "Tenant B Backup", "cmdb_ci_lb"),
            _ci("ci-server-b", "Tenant B Server", "cmdb_ci_server"),
        ],
        relationships=[
            _relationship(
                "rel-b-connects", "ci-app", "ci-lb-2",
                "Connects to::Connected by",
                parent_class="cmdb_ci_lb",
                child_class="cmdb_ci_lb",
            )
        ],
    )
    monkeypatch.setattr(sn, "is_live", lambda: True)
    _configure(org_a, ["cmdb_ci_server", "cmdb_ci_db_instance"])
    _configure(org_b, ["cmdb_ci_lb"])
    try:
        payload_a = _initial_load(org_a, client_a)
        payload_b = _initial_load(org_b, client_b)

        assert payload_a["class_scope"] == ["cmdb_ci_db_instance", "cmdb_ci_server"]
        assert {item["sys_id"] for item in payload_a["configuration_items"]} == {
            "ci-app", "ci-db", "ci-host"
        }
        assert all(
            set(item) == {
                "sys_id", "name", "ci_class", "operational_status",
                "assignment_group", "owned_by", "environment", "updated_at",
                "source_url",
            }
            for item in payload_a["configuration_items"]
        )
        assert "ci-printer" not in {
            entity.source_record_id for entity in list_source_entities(
                org_id=org_a, entity_type="system", source_system="servicenow"
            )
        }

        entity_rows = _rows(
            "SELECT * FROM entities WHERE org_id = %s ORDER BY source_record_id",
            (org_a,),
        )
        assert len(entity_rows) == 3
        for row in entity_rows:
            metadata = _json(row["metadata"])
            pointer = metadata["evidence_pointer"]
            assert metadata["ci_class"] in {"cmdb_ci_server", "cmdb_ci_db_instance"}
            assert metadata["operational_status"] == "Operational"
            assert metadata["assignment_group"] == "Platform Operations"
            assert metadata["owned_by"] == "Infrastructure"
            assert metadata["environment"] == "production"
            assert metadata["ci_sys_id"] == row["source_record_id"]
            assert metadata["source_url"].endswith(
                f"sys_id%3D{row['source_record_id']}"
            )
            parsed = urlsplit(metadata["source_url"])
            assert (parsed.scheme, parsed.hostname) == ("https", "acme.service-now.com")
            assert pointer["origin"] == "observed"
            assert pointer["source_system"] == "servicenow"
            assert pointer["source_artifact"] == row["source_record_id"]
            assert pointer["source_timestamp"] == metadata["source_updated_at"]

        relationship_rows = _rows(
            "SELECT * FROM entity_relationships WHERE org_id = %s",
            (org_a,),
        )
        assert {row["relationship_type"] for row in relationship_rows} == {
            "depends_on", "used_by", "runs_on", "connects_to"
        }
        assert len(relationship_rows) == 4
        for row in relationship_rows:
            evidence = _json(row["evidence"])
            assert row["inferred"] is False
            assert evidence["relationship_sys_id"] != "rel-outside"
            assert evidence["source"] == "servicenow"
            assert evidence["source_url"].endswith(
                f"sys_id%3D{evidence['relationship_sys_id']}"
            )
            assert evidence["evidence_pointer"]["origin"] == "observed"
            assert evidence["evidence_pointer"]["source_artifact"] == evidence[
                "relationship_sys_id"
            ]

        entity_a = get_source_entity(
            org_id=org_a, entity_type="system", source_system="servicenow",
            source_record_id="ci-app",
        )
        entity_b = get_source_entity(
            org_id=org_b, entity_type="system", source_system="servicenow",
            source_record_id="ci-app",
        )
        assert entity_a is not None and entity_b is not None
        assert str(entity_a.id) != str(entity_b.id)
        assert (entity_a.metadata or {})["ci_class"] == "cmdb_ci_server"
        assert (entity_b.metadata or {})["ci_class"] == "cmdb_ci_lb"
        cross_tenant_edges = _rows(
            "SELECT er.id FROM entity_relationships er "
            "JOIN entities source ON source.id = er.from_entity_id "
            "JOIN entities target ON target.id = er.to_entity_id "
            "WHERE er.org_id IN (%s, %s) "
            "AND (source.org_id <> er.org_id OR target.org_id <> er.org_id)",
            (org_a, org_b),
        )
        assert cross_tenant_edges == []
        assert len(_rows(
            "SELECT * FROM entity_relationships WHERE org_id = %s", (org_b,)
        )) == 1

        for client in (client_a, client_b):
            assert client.write_attempts == []
            assert {call["operation"] for call in client.read_calls} == {"table_query"}
            assert {call["table"] for call in client.read_calls} <= {
                "cmdb_ci", "cmdb_rel_ci", "sys_audit_delete"
            }
        forbidden_api = {
            "create_record", "update_record", "delete_record", "remediate",
            "reconcile", "run_data_quality",
        }
        assert forbidden_api.isdisjoint(sn.ServiceNowClient.__dict__)
    finally:
        _cleanup(org_a, org_b)


def test_incidents_resolve_only_from_primary_or_affected_ci_references(monkeypatch):
    org_id = f"msp-b3-incidents-{uuid4().hex}"
    client = _base_client()
    monkeypatch.setattr(sn, "is_live", lambda: True)
    _configure(org_id, ["cmdb_ci_server", "cmdb_ci_db_instance"])
    try:
        payload = _initial_load(org_id, client)
        entities = list_source_entities(
            org_id=org_id, entity_type="system", source_system="servicenow"
        )
        incidents = [
            {
                "sys_id": "incident-primary",
                "number": "INC001",
                "cmdb_ci": {"value": "ci-app", "display_value": "Citizen Portal"},
                "description": "Explicit reference wins.",
                "source_timestamp": "2026-07-15 09:30:00",
                "source_url": "https://acme.service-now.com/incident-primary",
            },
            {
                "sys_id": "incident-affected",
                "number": "INC002",
                "affected_ci_references": [{
                    "relationship_sys_id": "task-ci-001",
                    "incident_sys_id": "incident-affected",
                    "ci_sys_id": "ci-app",
                    "source_timestamp": "2026-07-15 09:31:00",
                    "source_url": "https://acme.service-now.com/task-ci-001",
                }],
            },
            {
                "sys_id": "incident-free-text",
                "number": "INC003",
                "description": "Citizen Portal is unavailable",
            },
        ]
        metrics = {
            "affected_ci_lookup": {"status": "available"},
            "incidents": incidents,
        }

        counts = resolve_incident_ci_references(
            org_id=org_id,
            incident_metrics=metrics,
            cmdb_entities=entities,
            cmdb_relationships=payload["relationships"],
        )

        assert counts == {"resolved": 2, "unresolved": 1}
        primary, affected, free_text = incidents
        assert primary["ci_resolution"]["method"] == "incident_cmdb_ci"
        assert affected["ci_resolution"]["method"] == "affected_ci_task"
        assert primary["ci_entity_id"] == affected["ci_entity_id"]
        assert affected["ci_resolution"]["supporting_relationship_ids"] == [
            "task-ci-001"
        ]
        assert free_text["ci_resolution"]["reason"] == "missing_explicit_reference"
        assert "ci_entity_id" not in free_text

        incident_hop = primary["ci_evidence_trace"]["incident_to_ci"]
        dependency_hops = primary["ci_evidence_trace"]["ci_dependencies"]
        assert incident_hop["origin"] == "observed"
        assert incident_hop["source_artifact"] == "incident-primary"
        assert incident_hop["source_url"].endswith("incident-primary")
        assert dependency_hops
        assert all(hop["origin"] == "observed" for hop in dependency_hops)
        assert all(hop["source_record_id"] for hop in dependency_hops)
        assert all(hop["source_url"].startswith("https://") for hop in dependency_hops)
    finally:
        _cleanup(org_id)


def test_incremental_delta_processes_changes_retirement_and_removal(monkeypatch):
    org_id = f"msp-b3-delta-{uuid4().hex}"
    client = _base_client()
    monkeypatch.setattr(sn, "is_live", lambda: True)
    _configure(org_id, ["cmdb_ci_server", "cmdb_ci_db_instance"])
    initial_run = f"run-{org_id}-initial"
    delta_run = f"run-{org_id}-delta"
    try:
        _initial_load(org_id, client)
        initial_db = get_source_entity(
            org_id=org_id, entity_type="system", source_system="servicenow",
            source_record_id="ci-db",
        )
        assert initial_db is not None

        client.cis = [
            _ci(
                "ci-app", "Citizen Portal", "cmdb_ci_server",
                updated_at="2026-07-15 11:00:00", status="Retired",
            ),
            # Unchanged: must be excluded by the sys_updated_on cursor.
            _ci("ci-db", "Citizen Database", "cmdb_ci_db_instance"),
            _ci(
                "ci-host", "app-prod-01", "cmdb_ci_server",
                updated_at="2026-07-15 11:05:00",
                assignment_group="Cloud Operations",
                owned_by="Runtime Engineering",
                environment="disaster-recovery",
            ),
            _ci("ci-printer", "Office Printer", "cmdb_ci_printer"),
        ]
        client.relationships = [
            # Unchanged observed relationships are not reread.
            next(r for r in _base_client().relationships if r["sys_id"] == "rel-used"),
            next(r for r in _base_client().relationships if r["sys_id"] == "rel-runs"),
            # Same source row changed label, so the old edge is replaced.
            _relationship(
                "rel-connects", "ci-db", "ci-host", "Depends on::Used by",
                parent_class="cmdb_ci_db_instance",
                child_class="cmdb_ci_server",
                updated_at="2026-07-15 11:15:00",
            ),
        ]
        client.relationship_deletions = [{
            "sys_id": "audit-delete-1",
            "documentkey": "rel-depends",
            "tablename": "cmdb_rel_ci",
            "sys_updated_on": "2026-07-15 11:10:00",
        }]
        client.read_calls.clear()

        delta = sn.ingest_cmdb_changes(
            org_id=org_id,
            run_id=delta_run,
            client=client,
            clock=lambda: DELTA_WATERMARK,
        )

        assert {item["sys_id"] for item in delta["configuration_items"]} == {
            "ci-app", "ci-host"
        }
        assert {item["sys_id"] for item in delta["relationships"]} == {
            "rel-connects"
        }
        assert {item["artifact_id"] for item in delta["relationship_deletions"]} == {
            "rel-depends"
        }
        assert all(
            "sys_updated_on>2026-07-15 10:00:00" in call["query"]
            for call in client.read_calls
        )

        app = get_source_entity(
            org_id=org_id, entity_type="system", source_system="servicenow",
            source_record_id="ci-app",
        )
        host = get_source_entity(
            org_id=org_id, entity_type="system", source_system="servicenow",
            source_record_id="ci-host",
        )
        unchanged_db = get_source_entity(
            org_id=org_id, entity_type="system", source_system="servicenow",
            source_record_id="ci-db",
        )
        assert app is not None and host is not None and unchanged_db is not None
        assert app.metadata["lifecycle_state"] == "retired"
        assert app.metadata["is_retired"] is True
        assert host.metadata["owner"] == "Runtime Engineering"
        assert host.metadata["environment"] == "disaster-recovery"
        assert str(unchanged_db.id) == str(initial_db.id)
        assert unchanged_db.last_seen_run_id == initial_run
        assert unchanged_db.run_count == 1

        graph_rows = _rows(
            "SELECT relationship_type, evidence FROM entity_relationships "
            "WHERE org_id = %s",
            (org_id,),
        )
        by_source = {
            _json(row["evidence"])["relationship_sys_id"]: row["relationship_type"]
            for row in graph_rows
        }
        assert "rel-depends" not in by_source
        assert by_source["rel-connects"] == "depends_on"
        assert by_source["rel-used"] == "used_by"
        assert by_source["rel-runs"] == "runs_on"

        ci_checkpoint = checkpoint_repository.read_checkpoint(
            org_id, sn.CMDB_CI_CHECKPOINT_ID
        )
        relationship_checkpoint = checkpoint_repository.read_checkpoint(
            org_id, sn.CMDB_RELATIONSHIP_CHECKPOINT_ID
        )
        assert ci_checkpoint is not None and relationship_checkpoint is not None
        assert ci_checkpoint.value == "2026-07-15 12:00:00"
        assert relationship_checkpoint.value == "2026-07-15 12:00:00"
        assert client.write_attempts == []
    finally:
        _cleanup(org_id)


def test_failed_relationship_processing_preserves_its_last_checkpoint(
    monkeypatch,
):
    org_id = f"msp-b3-failure-{uuid4().hex}"
    client = _base_client()
    monkeypatch.setattr(sn, "is_live", lambda: True)
    _configure(org_id, ["cmdb_ci_server", "cmdb_ci_db_instance"])
    try:
        _initial_load(org_id, client)
        client.cis = [
            _ci(
                "ci-app", "Citizen Portal", "cmdb_ci_server",
                updated_at="2026-07-15 11:00:00",
                assignment_group="Changed Team",
            )
        ]
        client.relationships = [
            _relationship(
                "rel-runs", "ci-app", "ci-host", "Depends on::Used by",
                parent_class="cmdb_ci_server",
                child_class="cmdb_ci_server",
                updated_at="2026-07-15 11:05:00",
            )
        ]

        import app.relationship_mapper as relationship_mapper

        def fail_graph_write(**kwargs):
            raise RuntimeError("deterministic relationship graph failure")

        monkeypatch.setattr(
            relationship_mapper,
            "apply_servicenow_cmdb_relationship_delta",
            fail_graph_write,
        )
        result = sn.ingest_cmdb_changes(
            org_id=org_id,
            run_id=f"run-{org_id}-failed",
            client=client,
            clock=lambda: DELTA_WATERMARK,
        )

        assert result["streams"]["cmdb_ci"]["checkpoint_advanced"] is True
        assert result["streams"]["cmdb_rel_ci"]["checkpoint_advanced"] is False
        assert "deterministic relationship graph failure" in result["streams"][
            "cmdb_rel_ci"
        ]["error"]
        assert checkpoint_repository.read_checkpoint(
            org_id, sn.CMDB_CI_CHECKPOINT_ID
        ).value == "2026-07-15 12:00:00"
        assert checkpoint_repository.read_checkpoint(
            org_id, sn.CMDB_RELATIONSHIP_CHECKPOINT_ID
        ).value == "2026-07-15 10:00:00"
        assert client.write_attempts == []
    finally:
        _cleanup(org_id)
