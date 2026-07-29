"""MSP-B3 T5: independent, failure-safe CMDB delta streams."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from discovery.ingest.base import Checkpoint
from discovery.ingest.change_runner import ingest_with_checkpoint
from discovery.ingest import servicenow as sn


NOW = datetime(2026, 7, 14, 12, 30, tzinfo=timezone.utc)


def _checkpoint(connector_id: str, value: str = "2026-07-14 10:00:00") -> Checkpoint:
    return Checkpoint.create(connector_id, "org-a", value)


def test_ci_delta_uses_sys_updated_on_and_projects_only_bounded_fields(monkeypatch):
    calls = []

    class ReadOnlyClient:
        instance_url = "https://acme.service-now.com"

        def table_query(self, table, params, max_records):
            calls.append((table, dict(params), max_records))
            return [{
                "sys_id": "ci-1",
                "name": "Application",
                "sys_class_name": "cmdb_ci_server",
                "operational_status": "Retired",
                "sys_updated_on": "2026-07-14 11:00:00",
            }]

    monkeypatch.setattr(sn, "is_live", lambda: True)
    ingestor = sn.ServiceNowCMDBCIChangeIngestor(
        org_id="org-a",
        class_scope=("cmdb_ci_server",),
        client=ReadOnlyClient(),
        clock=lambda: NOW,
    )

    batch = list(ingestor.ingest_changes("org-a", _checkpoint(ingestor.connector_id)))[0]

    assert calls[0][0] == "cmdb_ci"
    assert calls[0][1]["sysparm_query"] == (
        "sys_class_nameINcmdb_ci_server^"
        "sys_updated_on>2026-07-14 10:00:00^"
        "sys_updated_on<=2026-07-14 12:30:00^"
        "ORDERBYsys_updated_on^ORDERBYsys_id"
    )
    assert calls[0][1]["sysparm_fields"] == ",".join(sn.CMDB_FIELDS)
    assert calls[0][2] == sn.CMDB_RECORD_CAP
    assert batch.next_checkpoint == "2026-07-14 12:30:00"
    assert batch.records[0]["operational_status"] == "Retired"
    assert batch.records[0]["artifact_id"] == "ci-1"


def test_relationship_delta_reports_soft_and_hard_deletions_without_writes(monkeypatch):
    calls = []

    class ReadOnlyClient:
        instance_url = "https://acme.service-now.com"

        def table_query(self, table, params, max_records):
            calls.append((table, dict(params)))
            if table == "cmdb_rel_ci":
                return [{
                    "sys_id": "rel-soft",
                    "parent": "ci-1",
                    "child": "ci-2",
                    "type": "Depends on::Used by",
                    "active": "false",
                    "sys_updated_on": "2026-07-14 11:00:00",
                }]
            assert table == "sys_audit_delete"
            return [{
                "sys_id": "delete-1",
                "documentkey": "rel-hard",
                "tablename": "cmdb_rel_ci",
                "sys_updated_on": "2026-07-14 11:30:00",
            }]

    monkeypatch.setattr(sn, "is_live", lambda: True)
    ingestor = sn.ServiceNowCMDBRelationshipChangeIngestor(
        org_id="org-a",
        class_scope=("cmdb_ci_server",),
        admitted_ci_ids=frozenset({"ci-1", "ci-2"}),
        known_relationship_ids=frozenset({"rel-hard"}),
        client=ReadOnlyClient(),
        clock=lambda: NOW,
    )

    batch = list(ingestor.ingest_changes("org-a", _checkpoint(ingestor.connector_id)))[0]

    assert {record["artifact_id"] for record in batch.records} == {"rel-soft", "rel-hard"}
    assert {record["change_kind"] for record in batch.records} == {"deleted"}
    assert [table for table, _ in calls] == ["cmdb_rel_ci", "sys_audit_delete"]
    assert "parent.sys_class_nameINcmdb_ci_server" in calls[0][1]["sysparm_query"]
    assert "documentkeyINrel-hard" in calls[1][1]["sysparm_query"]


def test_stream_positions_are_distinct_and_partial_failure_preserves_relationship_cursor(monkeypatch):
    class ReadOnlyClient:
        instance_url = "https://acme.service-now.com"

        def table_query(self, table, params, max_records):
            if table == "cmdb_ci":
                return []
            if table == "cmdb_rel_ci":
                return [{
                    "sys_id": "rel-1",
                    "parent": "ci-1",
                    "child": "ci-2",
                    "type": "Depends on::Used by",
                    "active": "true",
                    "sys_updated_on": "2026-07-14 11:00:00",
                }]
            return []

    monkeypatch.setattr(sn, "is_live", lambda: True)
    ci = sn.ServiceNowCMDBCIChangeIngestor(
        org_id="org-a", class_scope=("cmdb_ci_server",),
        client=ReadOnlyClient(), clock=lambda: NOW,
    )
    rel = sn.ServiceNowCMDBRelationshipChangeIngestor(
        org_id="org-a", class_scope=("cmdb_ci_server",),
        admitted_ci_ids=frozenset({"ci-1", "ci-2"}),
        client=ReadOnlyClient(), clock=lambda: NOW,
    )
    assert ci.connector_id != rel.connector_id

    stored = {
        ci.connector_id: _checkpoint(ci.connector_id),
        rel.connector_id: _checkpoint(rel.connector_id),
    }

    def read_checkpoint(org_id, connector_id):
        assert org_id == "org-a"
        return stored.get(connector_id)

    def save_checkpoint(checkpoint):
        stored[checkpoint.connector_id] = checkpoint

    ci_result = ingest_with_checkpoint(
        ci, "org-a", read_checkpoint=read_checkpoint, save_checkpoint=save_checkpoint
    )
    rel_result = ingest_with_checkpoint(
        rel,
        "org-a",
        process_batch=lambda batch: (_ for _ in ()).throw(RuntimeError("graph write failed")),
        read_checkpoint=read_checkpoint,
        save_checkpoint=save_checkpoint,
    )

    assert ci_result.ok and ci_result.checkpoint_advanced
    assert stored[ci.connector_id].value == "2026-07-14 12:30:00"
    assert not rel_result.ok and not rel_result.checkpoint_advanced
    assert stored[rel.connector_id].value == "2026-07-14 10:00:00"


def test_invalid_checkpoint_is_rejected_before_any_servicenow_request(monkeypatch):
    class Client:
        def table_query(self, *args, **kwargs):
            raise AssertionError("invalid cursor must not reach ServiceNow")

    monkeypatch.setattr(sn, "is_live", lambda: True)
    ingestor = sn.ServiceNowCMDBCIChangeIngestor(
        org_id="org-a", class_scope=("cmdb_ci_server",), client=Client()
    )
    malicious = _checkpoint(ingestor.connector_id, "2026-07-14 10:00:00^ORnameLIKEsecret")
    with pytest.raises(sn.ServiceNowIngestError, match="invalid.*checkpoint"):
        list(ingestor.ingest_changes("org-a", malicious))
