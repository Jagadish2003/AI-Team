"""Contract coverage for MSP-B3 T2's bounded observed-edge handoff."""
from __future__ import annotations

from discovery.ingest import clear_live_connectors, set_ingest_org
from discovery.ingest import servicenow as sn


def test_cmdb_relationship_contract_is_bounded_normalized_and_observed(monkeypatch):
    class ReadOnlyServiceNowClient:
        instance_url = "https://acme.service-now.com"

        def table_query(self, table, params, max_records):
            if table == "cmdb_ci":
                return [
                    {
                        "sys_id": "ci-1",
                        "name": "Application",
                        "sys_class_name": "cmdb_ci_service_auto",
                    },
                    {
                        "sys_id": "ci-2",
                        "name": "Compute",
                        "sys_class_name": "cmdb_ci_server",
                    },
                ]
            assert "parent.sys_class_nameIN" in params["sysparm_query"]
            assert "child.sys_class_nameIN" in params["sysparm_query"]
            return [
                {
                    "sys_id": "rel-admitted",
                    "parent": {"value": "ci-1", "display_value": "Application"},
                    "child": {"value": "ci-2", "display_value": "Compute"},
                    "type": {
                        "value": "type-runs-on",
                        "display_value": "Runs on::Runs",
                    },
                    "sys_updated_on": "2026-07-14 12:00:00",
                },
                {
                    "sys_id": "rel-rejected",
                    "parent": {"value": "ci-1", "display_value": "Application"},
                    "child": {"value": "ci-outside", "display_value": "Secret"},
                    "type": {
                        "value": "type-depends-on",
                        "display_value": "Depends on::Used by",
                    },
                    "sys_updated_on": "2026-07-14 12:01:00",
                },
            ]

    clear_live_connectors()
    monkeypatch.setattr(sn, "is_live", lambda: True)
    monkeypatch.setattr(
        sn,
        "_load_org_cmdb_config",
        lambda org_id: ["cmdb_ci_service_auto", "cmdb_ci_server"],
    )
    set_ingest_org("org-contract")
    try:
        payload = sn.ingest_cmdb(ReadOnlyServiceNowClient())
    finally:
        clear_live_connectors()

    assert payload["org_id"] == "org-contract"
    assert payload["relationships"] == [
        {
            "sys_id": "rel-admitted",
            "relationship_type": "runs_on",
            "source_ci_id": "ci-1",
            "target_ci_id": "ci-2",
            "servicenow_parent_id": "ci-1",
            "servicenow_child_id": "ci-2",
            "source_relationship_name": "Runs on::Runs",
            "source_type": "servicenow_cmdb_rel_ci",
            "source_timestamp": "2026-07-14 12:00:00",
            "source_url": (
                "https://acme.service-now.com/nav_to.do?"
                "uri=cmdb_rel_ci.do%3Fsys_id%3Drel-admitted"
            ),
            "origin": "observed",
        }
    ]
