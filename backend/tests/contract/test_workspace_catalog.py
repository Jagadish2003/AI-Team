from __future__ import annotations

import os
from typing import Any, Dict, List

from fastapi import FastAPI

from app import db as _db
from app import routes_workspace_catalog as catalog


PATH = catalog.WORKSPACE_CATALOG_PATH
CATEGORIES = [
    "primary_platforms",
    "operational_systems",
    "comms_knowledge",
    "data_engineering",
    "cloud_operations",
]


def _auth() -> Dict[str, str]:
    return {"Authorization": f"Bearer {os.getenv('DEV_JWT', 'dev-token-change-me')}"}


def _connector(
    system_id: str,
    name: str,
    status: str,
    products: Any = None,
) -> Dict[str, Any]:
    record = {"id": system_id, "name": name, "status": status}
    if products is not None:
        record["products"] = products
    return record


def _install_connectors(monkeypatch, connectors: List[Dict[str, Any]]) -> None:
    # The catalog route now reads connectors through db.org_connectors_list,
    # which is org-scoped and calls db.get_all("connectors") internally. The
    # injected rows carry no org_id, so they are treated as shared-catalog rows
    # and surface for the (default) request org. Patch the db-level get_all so
    # the org-scoped merge sees the fixture data.
    def fake_get_all(table: str) -> List[Dict[str, Any]]:
        assert table == "connectors"
        return connectors

    monkeypatch.setattr(_db, "get_all", fake_get_all)


def _get(client):
    return client.get(PATH, headers=_auth())


def _ids(data: Dict[str, Any], category: str) -> List[str]:
    return [item["system_id"] for item in data[category]]


def _item(data: Dict[str, Any], category: str, system_id: str) -> Dict[str, Any]:
    return next(item for item in data[category] if item["system_id"] == system_id)


class TestCatalogStructure:
    def test_returns_200_with_all_keys(self, client, monkeypatch):
        _install_connectors(monkeypatch, [])

        response = _get(client)

        assert response.status_code == 200
        assert set(response.json()) == {*CATEGORIES, "missing_categories"}

    def test_category_values_are_lists(self, client, monkeypatch):
        _install_connectors(monkeypatch, [])

        data = _get(client).json()

        for key in [*CATEGORIES, "missing_categories"]:
            assert isinstance(data[key], list)

    def test_empty_workspace_marks_all_categories_missing(self, client, monkeypatch):
        _install_connectors(monkeypatch, [])

        data = _get(client).json()

        assert data["missing_categories"] == CATEGORIES

    def test_system_item_shape(self, client, monkeypatch):
        _install_connectors(monkeypatch, [_connector("jira", "Jira", "connected")])

        data = _get(client).json()
        jira = _item(data, "operational_systems", "jira")

        assert set(jira) == {"system_id", "name", "status", "products"}
        assert jira["products"] == []

    def test_requires_auth(self, client, monkeypatch):
        _install_connectors(monkeypatch, [])

        response = client.get(PATH)

        assert response.status_code == 401

    def test_openapi_uses_integration_hub_tag(self, client):
        response = client.get("/openapi.json")

        assert response.status_code == 200
        assert response.json()["paths"][PATH]["get"]["tags"] == ["Integration Hub"]


class TestCategoryRouting:
    def test_salesforce_in_primary_platforms(self, client, monkeypatch):
        _install_connectors(
            monkeypatch,
            [_connector("salesforce", "Salesforce", "connected")],
        )

        data = _get(client).json()

        assert "salesforce" in _ids(data, "primary_platforms")

    def test_jira_in_operational_systems(self, client, monkeypatch):
        _install_connectors(monkeypatch, [_connector("jira", "Jira", "connected")])

        data = _get(client).json()

        assert "jira" in _ids(data, "operational_systems")

    def test_servicenow_in_operational_systems(self, client, monkeypatch):
        _install_connectors(
            monkeypatch,
            [_connector("servicenow", "ServiceNow", "needs_auth")],
        )

        data = _get(client).json()

        assert "servicenow" in _ids(data, "operational_systems")

    def test_slack_in_comms_knowledge(self, client, monkeypatch):
        _install_connectors(monkeypatch, [_connector("slack", "Slack", "connected")])

        data = _get(client).json()

        assert "slack" in _ids(data, "comms_knowledge")

    def test_github_in_data_engineering(self, client, monkeypatch):
        _install_connectors(monkeypatch, [_connector("github", "GitHub", "connected")])

        data = _get(client).json()

        assert "github" in _ids(data, "data_engineering")

    def test_multiple_categories_route_together(self, client, monkeypatch):
        _install_connectors(
            monkeypatch,
            [
                _connector("salesforce", "Salesforce", "connected"),
                _connector("jira", "Jira", "connected"),
                _connector("slack", "Slack", "needs_auth"),
                _connector("snowflake", "Snowflake", "connected"),
            ],
        )

        data = _get(client).json()

        assert _ids(data, "primary_platforms") == ["salesforce"]
        assert _ids(data, "operational_systems") == ["jira"]
        assert _ids(data, "comms_knowledge") == ["slack"]
        assert _ids(data, "data_engineering") == ["snowflake"]


class TestCloudOperationsRouting:
    """MSP-B13: multi-account/subscription cloud connectors route to the
    cloud_operations bucket via the connector's ``multiScope`` flag — the same
    registry-driven rule the Integration Hub uses — not a hardcoded id list."""

    def test_azure_events_in_cloud_operations(self, client, monkeypatch):
        _install_connectors(
            monkeypatch,
            [{"id": "azure_events", "name": "Azure Events",
              "status": "connected", "multiScope": True}],
        )

        data = _get(client).json()

        assert "azure_events" in _ids(data, "cloud_operations")

    def test_aws_events_in_cloud_operations(self, client, monkeypatch):
        _install_connectors(
            monkeypatch,
            [{"id": "aws_events", "name": "AWS Events",
              "status": "needs_auth", "multiScope": True}],
        )

        data = _get(client).json()

        assert "aws_events" in _ids(data, "cloud_operations")

    def test_multiscope_flag_drives_membership_not_id(self, client, monkeypatch):
        # A future cloud connector with an unknown id still buckets here purely
        # from the multiScope flag — no id list to maintain.
        _install_connectors(
            monkeypatch,
            [{"id": "gcp_events", "name": "GCP Events",
              "status": "connected", "multiScope": True}],
        )

        data = _get(client).json()

        assert "gcp_events" in _ids(data, "cloud_operations")

    def test_connected_cloud_connector_clears_missing(self, client, monkeypatch):
        _install_connectors(
            monkeypatch,
            [{"id": "azure_events", "name": "Azure Events",
              "status": "connected", "multiScope": True}],
        )

        data = _get(client).json()

        assert "cloud_operations" not in data["missing_categories"]

    def test_not_configured_cloud_connector_is_excluded(self, client, monkeypatch):
        _install_connectors(
            monkeypatch,
            [{"id": "azure_events", "name": "Azure Events",
              "status": "not_configured", "multiScope": True}],
        )

        data = _get(client).json()

        assert data["cloud_operations"] == []
        assert "cloud_operations" in data["missing_categories"]


class TestStatusNormalization:
    def test_live_status_normalises_to_connected(self, client, monkeypatch):
        _install_connectors(monkeypatch, [_connector("jira", "Jira", "live")])

        data = _get(client).json()

        assert _item(data, "operational_systems", "jira")["status"] == "connected"

    def test_fixture_status_normalises_to_connected(self, client, monkeypatch):
        _install_connectors(monkeypatch, [_connector("github", "GitHub", "fixture")])

        data = _get(client).json()

        assert _item(data, "data_engineering", "github")["status"] == "connected"

    def test_error_status_normalises_to_needs_auth(self, client, monkeypatch):
        _install_connectors(monkeypatch, [_connector("slack", "Slack", "error")])

        data = _get(client).json()

        assert _item(data, "comms_knowledge", "slack")["status"] == "needs_auth"

    def test_disconnected_status_is_excluded(self, client, monkeypatch):
        _install_connectors(monkeypatch, [_connector("salesforce", "Salesforce", "disconnected")])

        data = _get(client).json()

        assert data["primary_platforms"] == []

    def test_not_connected_status_is_excluded(self, client, monkeypatch):
        _install_connectors(monkeypatch, [_connector("jira", "Jira", "not_connected")])

        data = _get(client).json()

        assert data["operational_systems"] == []

    def test_unknown_status_is_excluded(self, client, monkeypatch):
        _install_connectors(monkeypatch, [_connector("github", "GitHub", "pending")])

        data = _get(client).json()

        assert data["data_engineering"] == []


class TestMissingCategories:
    def test_connected_counts_as_present(self, client, monkeypatch):
        _install_connectors(monkeypatch, [_connector("jira", "Jira", "connected")])

        data = _get(client).json()

        assert "operational_systems" not in data["missing_categories"]

    def test_needs_auth_counts_as_present(self, client, monkeypatch):
        _install_connectors(monkeypatch, [_connector("slack", "Slack", "needs_auth")])

        data = _get(client).json()

        assert "comms_knowledge" not in data["missing_categories"]

    def test_not_configured_does_not_count_as_present(self, client, monkeypatch):
        _install_connectors(monkeypatch, [_connector("slack", "Slack", "not_configured")])

        data = _get(client).json()

        assert data["comms_knowledge"] == []
        assert "comms_knowledge" in data["missing_categories"]

    def test_only_empty_categories_are_missing(self, client, monkeypatch):
        _install_connectors(
            monkeypatch,
            [
                _connector("salesforce", "Salesforce", "connected"),
                _connector("servicenow", "ServiceNow", "needs_auth"),
            ],
        )

        data = _get(client).json()

        assert data["missing_categories"] == [
            "comms_knowledge",
            "data_engineering",
            "cloud_operations",
        ]

    def test_not_configured_peer_does_not_make_present_category_missing(
        self,
        client,
        monkeypatch,
    ):
        _install_connectors(
            monkeypatch,
            [
                _connector("jira", "Jira", "connected"),
                _connector("servicenow", "ServiceNow", "not_configured"),
            ],
        )

        data = _get(client).json()

        assert _ids(data, "operational_systems") == ["jira"]
        assert "operational_systems" not in data["missing_categories"]


class TestSalesforceProducts:
    def test_salesforce_products_are_populated(self, client, monkeypatch):
        _install_connectors(
            monkeypatch,
            [
                _connector(
                    "salesforce",
                    "Salesforce",
                    "connected",
                    products=["salesforce_pss", "salesforce_sc"],
                )
            ],
        )

        data = _get(client).json()
        salesforce = _item(data, "primary_platforms", "salesforce")

        assert salesforce["products"] == ["salesforce_pss", "salesforce_sc"]

    def test_salesforce_products_default_to_empty_list(self, client, monkeypatch):
        _install_connectors(monkeypatch, [_connector("salesforce", "Salesforce", "connected")])

        data = _get(client).json()
        salesforce = _item(data, "primary_platforms", "salesforce")

        assert salesforce["products"] == []

    def test_non_list_products_are_sanitized(self, client, monkeypatch):
        _install_connectors(
            monkeypatch,
            [_connector("salesforce", "Salesforce", "connected", products="salesforce_pss")],
        )

        data = _get(client).json()
        salesforce = _item(data, "primary_platforms", "salesforce")

        assert salesforce["products"] == []

    def test_non_salesforce_products_are_empty(self, client, monkeypatch):
        _install_connectors(
            monkeypatch,
            [_connector("jira", "Jira", "connected", products=["unexpected_product"])],
        )

        data = _get(client).json()
        jira = _item(data, "operational_systems", "jira")

        assert jira["products"] == []


class TestRemovedAndUnknownSystems:
    def test_neospin_not_in_response(self, client, monkeypatch):
        _install_connectors(monkeypatch, [_connector("neospin", "Neospin", "connected")])

        data = _get(client).json()

        assert all("neospin" not in _ids(data, category) for category in CATEGORIES)

    def test_vitech_not_in_response(self, client, monkeypatch):
        _install_connectors(monkeypatch, [_connector("vitech", "Vitech", "connected")])

        data = _get(client).json()

        assert all("vitech" not in _ids(data, category) for category in CATEGORIES)

    def test_unknown_system_ids_are_skipped(self, client, monkeypatch):
        _install_connectors(
            monkeypatch,
            [_connector("future_system", "Future System", "connected")],
        )

        data = _get(client).json()

        assert all(data[category] == [] for category in CATEGORIES)


class TestRouteRegistration:
    def test_register_workspace_catalog_routes_is_idempotent(self):
        test_app = FastAPI()

        catalog.register_workspace_catalog_routes(test_app)
        catalog.register_workspace_catalog_routes(test_app)

        matches = [
            route
            for route in test_app.routes
            if getattr(route, "path", None) == PATH
        ]
        assert len(matches) == 1
