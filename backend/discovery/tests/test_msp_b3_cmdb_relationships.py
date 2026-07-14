"""MSP-B3 T2: bounded ServiceNow CMDB relationship ingestion."""
from __future__ import annotations

import pytest

from discovery.ingest import clear_live_connectors, set_ingest_org
from discovery.ingest import servicenow as sn


@pytest.fixture(autouse=True)
def _clear_ingest_context():
    clear_live_connectors()
    yield
    clear_live_connectors()


def _ci(sys_id: str, ci_class: str = "cmdb_ci_server"):
    return {
        "sys_id": sys_id,
        "name": sys_id,
        "sys_class_name": ci_class,
        "operational_status": "Operational",
        "sys_updated_on": "2026-07-14 08:00:00",
    }


def _rel(
    sys_id: str,
    parent: str,
    child: str,
    label: str,
    timestamp: str = "2026-07-14 09:00:00",
):
    return {
        "sys_id": sys_id,
        "parent": {"value": parent, "display_value": f"name-{parent}"},
        "child": {"value": child, "display_value": f"name-{child}"},
        "type": {"value": f"type-{sys_id}", "display_value": label},
        "sys_updated_on": timestamp,
        "connection_info": "must-not-cross-boundary",
        "password": "must-not-cross-boundary",
    }


def test_relationship_read_is_server_bounded_then_exact_id_bounded(monkeypatch):
    calls = []

    class ReadOnlyClient:
        def table_query(self, table, params, max_records):
            calls.append((table, dict(params), max_records))
            if table == "cmdb_ci":
                return [_ci("ci-app", "cmdb_ci_service_auto"), _ci("ci-server")]
            assert table == "cmdb_rel_ci"
            return [
                _rel("rel-in-scope", "ci-app", "ci-server", "Runs on::Runs"),
                _rel("rel-outside", "ci-app", "ci-secret", "Depends on::Used by"),
                _rel("rel-unknown", "ci-server", "ci-app", "Receives data from"),
            ]

    monkeypatch.setattr(sn, "is_live", lambda: True)
    monkeypatch.setattr(
        sn,
        "_load_org_cmdb_config",
        lambda org_id: ["cmdb_ci_server", "cmdb_ci_service_auto"],
    )
    set_ingest_org("org-bounded")

    edges = sn.get_cmdb_relationships(ReadOnlyClient())

    assert [edge.sys_id for edge in edges] == ["rel-in-scope"]
    relationship_call = calls[1]
    assert relationship_call[0] == "cmdb_rel_ci"
    query = relationship_call[1]["sysparm_query"]
    class_csv = "cmdb_ci_server,cmdb_ci_service_auto"
    assert f"parent.sys_class_nameIN{class_csv}" in query
    assert f"child.sys_class_nameIN{class_csv}" in query
    assert relationship_call[1]["sysparm_fields"] == ",".join(
        sn.CMDB_RELATIONSHIP_FIELDS
    )
    assert relationship_call[1]["sysparm_display_value"] == "all"
    assert relationship_call[1]["sysparm_exclude_reference_link"] == "true"
    assert relationship_call[2] == sn.CMDB_RECORD_CAP


@pytest.mark.parametrize(
    ("label", "relationship_type", "source", "target"),
    [
        ("Depends on::Used by", "depends_on", "parent", "child"),
        ("Used by", "used_by", "child", "parent"),
        ("Runs on::Runs", "runs_on", "parent", "child"),
        ("Runs", "runs_on", "child", "parent"),
        ("Connects to::Connected by", "connects_to", "parent", "child"),
        ("Connected by", "connects_to", "child", "parent"),
    ],
)
def test_central_rules_normalize_vocabulary_and_preserve_direction(
    label, relationship_type, source, target
):
    edge = sn._relationship_from_record(
        _rel("rel-1", "parent", "child", label),
        frozenset({"parent", "child"}),
    )

    assert edge is not None
    assert edge.relationship_type == relationship_type
    assert (edge.source_ci_id, edge.target_ci_id) == (source, target)


def test_relationship_representation_has_stable_observed_provenance_only():
    edge = sn._relationship_from_record(
        _rel(
            "rel-42",
            "ci-parent",
            "ci-child",
            "Depends on::Used by",
            "2026-07-14 09:42:00",
        ),
        frozenset({"ci-parent", "ci-child"}),
    )

    assert edge is not None
    assert edge.as_dict() == {
        "sys_id": "rel-42",
        "relationship_type": "depends_on",
        "source_ci_id": "ci-parent",
        "target_ci_id": "ci-child",
        "servicenow_parent_id": "ci-parent",
        "servicenow_child_id": "ci-child",
        "source_relationship_name": "Depends on::Used by",
        "source_type": "servicenow_cmdb_rel_ci",
        "source_timestamp": "2026-07-14 09:42:00",
        "source_url": None,
        "origin": "observed",
    }
    assert "password" not in edge.as_dict()
    assert "connection_info" not in edge.as_dict()


def test_mapping_can_be_extended_centrally_without_ingestion_string_logic():
    rules = {
        **sn.DEFAULT_CMDB_RELATIONSHIP_RULES,
        "Feeds": sn.CMDBRelationshipRule("connects_to"),
    }
    edge = sn._relationship_from_record(
        _rel("rel-feed", "ci-a", "ci-b", "FEEDS"),
        frozenset({"ci-a", "ci-b"}),
        rules,
    )
    assert edge is not None
    assert edge.relationship_type == "connects_to"


def test_mapping_extension_cannot_escape_the_bounded_graph_vocabulary():
    with pytest.raises(ValueError, match="unsupported CMDB graph relationship type"):
        sn.normalize_cmdb_relationship_type(
            "Invents",
            {"invents": sn.CMDBRelationshipRule("guessed_from_names")},
        )


def test_relationships_are_deterministically_sorted(monkeypatch):
    class Client:
        def table_query(self, table, params, max_records):
            if table == "cmdb_ci":
                return [_ci("ci-a"), _ci("ci-b"), _ci("ci-c")]
            return [
                _rel("rel-z", "ci-c", "ci-a", "Depends on::Used by"),
                _rel("rel-a", "ci-a", "ci-b", "Connects to::Connected by"),
                _rel("rel-b", "ci-a", "ci-c", "Depends on::Used by"),
            ]

    monkeypatch.setattr(sn, "is_live", lambda: True)
    monkeypatch.setattr(
        sn, "_load_org_cmdb_config", lambda org_id: ["cmdb_ci_server"]
    )
    set_ingest_org("org-order")

    assert [edge.sys_id for edge in sn.get_cmdb_relationships(Client())] == [
        "rel-a",
        "rel-b",
        "rel-z",
    ]


def test_current_org_scope_controls_both_endpoint_filters(monkeypatch):
    calls = []

    class Client:
        def table_query(self, table, params, max_records):
            calls.append((table, params["sysparm_query"]))
            if table == "cmdb_ci":
                ci_class = (
                    "cmdb_ci_server"
                    if "cmdb_ci_server" in params["sysparm_query"]
                    else "cmdb_ci_lb"
                )
                return [_ci(f"{ci_class}-a", ci_class), _ci(f"{ci_class}-b", ci_class)]
            return []

    configured = {
        "org-a": ["cmdb_ci_server"],
        "org-b": ["cmdb_ci_lb"],
    }
    monkeypatch.setattr(sn, "is_live", lambda: True)
    monkeypatch.setattr(sn, "_load_org_cmdb_config", lambda org_id: configured[org_id])

    set_ingest_org("org-a")
    sn.get_cmdb_relationships(Client())
    set_ingest_org("org-b")
    sn.get_cmdb_relationships(Client())

    relationship_queries = [query for table, query in calls if table == "cmdb_rel_ci"]
    assert relationship_queries == [
        "parent.sys_class_nameINcmdb_ci_server^"
        "child.sys_class_nameINcmdb_ci_server^ORDERBYsys_id",
        "parent.sys_class_nameINcmdb_ci_lb^"
        "child.sys_class_nameINcmdb_ci_lb^ORDERBYsys_id",
    ]


def test_offline_combined_payload_contains_only_explicit_fixture_edges(monkeypatch):
    monkeypatch.setattr(sn, "is_live", lambda: False)
    monkeypatch.setattr(sn, "_load_org_cmdb_config", lambda org_id: None)
    set_ingest_org("org-offline")

    payload = sn.ingest_cmdb()

    assert {edge["sys_id"] for edge in payload["relationships"]} == {
        "rel-app-runs-server-001",
        "rel-server-depends-db-001",
        "rel-lb-connects-network-001",
    }
    admitted = {item["sys_id"] for item in payload["configuration_items"]}
    assert all(
        edge["servicenow_parent_id"] in admitted
        and edge["servicenow_child_id"] in admitted
        for edge in payload["relationships"]
    )
    assert all(edge["source_url"].startswith("https://example.service-now.com/")
               for edge in payload["relationships"])


def test_empty_scope_performs_no_servicenow_read(monkeypatch):
    class Client:
        def table_query(self, *args, **kwargs):
            raise AssertionError("disabled scope must not read any CMDB table")

    monkeypatch.setattr(sn, "is_live", lambda: True)
    monkeypatch.setattr(sn, "_load_org_cmdb_config", lambda org_id: [])
    set_ingest_org("org-disabled")
    assert sn.get_cmdb_relationships(Client()) == []
