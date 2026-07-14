"""MSP-B3 T1: bounded, organization-scoped ServiceNow CMDB ingestion."""
from __future__ import annotations

import pytest

from discovery.ingest import clear_live_connectors, set_ingest_org
from discovery.ingest import servicenow as sn


@pytest.fixture(autouse=True)
def _clear_ingest_context():
    clear_live_connectors()
    yield
    clear_live_connectors()


def test_default_scope_covers_the_six_bounded_estate_categories(monkeypatch):
    monkeypatch.setattr(sn, "_load_org_cmdb_config", lambda org_id: None)
    set_ingest_org("org-default")

    assert sn.resolve_cmdb_class_scope() == tuple(sorted({
        "cmdb_ci_service_auto",
        "cmdb_ci_server",
        "cmdb_ci_db_instance",
        "cmdb_ci_storage_device",
        "cmdb_ci_netgear",
        "cmdb_ci_lb",
    }))


def test_org_can_narrow_extend_or_disable_scope_without_source_changes(monkeypatch):
    configured = {
        "org-narrow": ["cmdb_ci_server"],
        "org-extend": [*sn.DEFAULT_CMDB_CLASSES, "cmdb_ci_container"],
        "org-disabled": [],
    }
    monkeypatch.setattr(
        sn,
        "_load_org_cmdb_config",
        lambda org_id: configured.get(org_id),
    )

    set_ingest_org("org-narrow")
    assert sn.resolve_cmdb_class_scope() == ("cmdb_ci_server",)
    set_ingest_org("org-extend")
    assert "cmdb_ci_container" in sn.resolve_cmdb_class_scope()
    set_ingest_org("org-disabled")
    assert sn.resolve_cmdb_class_scope() == ()


def test_effective_scope_always_uses_current_ingest_org(monkeypatch):
    calls = []
    configured = {
        "org-a": ["cmdb_ci_server"],
        "org-b": ["cmdb_ci_lb"],
    }

    def load(org_id):
        calls.append(org_id)
        return configured[org_id]

    monkeypatch.setattr(sn, "_load_org_cmdb_config", load)
    set_ingest_org("org-a")
    assert sn.resolve_cmdb_class_scope() == ("cmdb_ci_server",)
    set_ingest_org("org-b")
    assert sn.resolve_cmdb_class_scope() == ("cmdb_ci_lb",)
    assert calls == ["org-a", "org-b"]


def test_configuration_read_failure_fails_closed(monkeypatch):
    def unavailable(org_id):
        raise sn.ServiceNowIngestError("configuration store unavailable")

    monkeypatch.setattr(sn, "_load_org_cmdb_config", unavailable)
    set_ingest_org("org-narrowed")
    with pytest.raises(sn.ServiceNowIngestError, match="configuration store"):
        sn.resolve_cmdb_class_scope()


@pytest.mark.parametrize(
    "invalid",
    ["cmdb_ci_server", ["incident"], ["cmdb_ci_server^ORnameLIKEsecret"], [123]],
)
def test_scope_rejects_non_class_or_encoded_query_input(invalid):
    with pytest.raises(ValueError):
        sn.normalize_cmdb_class_scope(invalid)


def test_live_path_server_filters_projects_fields_and_returns_stable_items(monkeypatch):
    captured = {}

    class Client:
        def table_query(self, table, params, max_records):
            captured.update(table=table, params=dict(params), max_records=max_records)
            return [
                {
                    "sys_id": {"value": "ci-2", "display_value": "ci-2"},
                    "name": "Zulu Database",
                    "sys_class_name": "cmdb_ci_db_instance",
                    "operational_status": {"value": "1", "display_value": "Operational"},
                    "assignment_group": {"value": "group-2", "display_value": "DB Operations"},
                    "owned_by": {"value": "user-2", "display_value": "Data Platform"},
                    "environment": "production",
                    "sys_updated_on": "2026-07-13 11:00:00",
                    "discovery_credentials": "must-not-cross-boundary",
                    "ip_address": "192.0.2.10",
                },
                {
                    "sys_id": "ci-1",
                    "name": "Alpha Server",
                    "sys_class_name": "cmdb_ci_server",
                    "operational_status": "Operational",
                    "assignment_group": "Compute Operations",
                    "owned_by": None,
                    "environment": "production",
                    "sys_updated_on": "2026-07-13 10:00:00",
                    "password": "must-not-cross-boundary",
                },
            ]

    monkeypatch.setattr(sn, "is_live", lambda: True)
    monkeypatch.setattr(
        sn,
        "_load_org_cmdb_config",
        lambda org_id: ["cmdb_ci_server", "cmdb_ci_db_instance"],
    )
    set_ingest_org("org-live")

    items = sn.get_cmdb_configuration_items(Client())

    assert captured["table"] == "cmdb_ci"
    assert captured["max_records"] == sn.CMDB_RECORD_CAP
    assert captured["params"]["sysparm_query"].startswith(
        "sys_class_nameINcmdb_ci_db_instance,cmdb_ci_server^"
    )
    assert captured["params"]["sysparm_fields"] == ",".join(sn.CMDB_FIELDS)
    assert captured["params"]["sysparm_display_value"] == "all"
    assert captured["params"]["sysparm_exclude_reference_link"] == "true"

    assert [item.sys_id for item in items] == ["ci-2", "ci-1"]
    db_item = items[0].as_dict()
    assert db_item == {
        "sys_id": "ci-2",
        "name": "Zulu Database",
        "ci_class": "cmdb_ci_db_instance",
        "operational_status": "Operational",
        "assignment_group": "DB Operations",
        "owned_by": "Data Platform",
        "environment": "production",
        "updated_at": "2026-07-13 11:00:00",
    }
    assert "password" not in db_item
    assert "discovery_credentials" not in db_item
    assert "ip_address" not in db_item


def test_empty_org_scope_performs_no_servicenow_request(monkeypatch):
    class Client:
        def table_query(self, *args, **kwargs):
            raise AssertionError("disabled CMDB scope must not query ServiceNow")

    monkeypatch.setattr(sn, "is_live", lambda: True)
    monkeypatch.setattr(sn, "_load_org_cmdb_config", lambda org_id: [])
    set_ingest_org("org-disabled")
    assert sn.get_cmdb_configuration_items(Client()) == []


def test_out_of_scope_server_response_fails_closed(monkeypatch):
    class Client:
        def table_query(self, table, params, max_records):
            return [{
                "sys_id": "ci-printer-1",
                "name": "Office Printer",
                "sys_class_name": "cmdb_ci_printer",
            }]

    monkeypatch.setattr(sn, "is_live", lambda: True)
    monkeypatch.setattr(
        sn, "_load_org_cmdb_config", lambda org_id: ["cmdb_ci_server"]
    )
    set_ingest_org("org-bounded")
    with pytest.raises(sn.ServiceNowIngestError, match="out-of-scope"):
        sn.get_cmdb_configuration_items(Client())


def test_offline_fixture_uses_same_stable_representation(monkeypatch):
    monkeypatch.setattr(sn, "is_live", lambda: False)
    monkeypatch.setattr(sn, "_load_org_cmdb_config", lambda org_id: None)
    set_ingest_org("org-offline")

    payload = sn.ingest_cmdb()

    assert payload["org_id"] == "org-offline"
    assert payload["class_scope"] == list(sn.DEFAULT_CMDB_CLASSES)
    assert len(payload["configuration_items"]) == 6
    assert set(payload["configuration_items"][0]) == {
        "sys_id", "name", "ci_class", "operational_status",
        "assignment_group", "owned_by", "environment", "updated_at",
    }
