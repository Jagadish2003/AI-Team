"""
test_sharepoint_sites.py — SharePoint multi-site selection contract tests.

Runs offline (INGEST_MODE unset → fixture): selectable site S-eng (Engineering).
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app import db

client = TestClient(app)

SITE = "S-eng"


@pytest.fixture(autouse=True, scope="module")
def seed_dev_owner():
    from app.rbac import seed_owner, _ensure_members_table
    _ensure_members_table()
    seed_owner("default", "dev-token-change-me")


def auth():
    return {"Authorization": "Bearer dev-token-change-me"}


def _set_status(status: str):
    connector = db.org_connector_get("default", "sharepoint")
    if not connector:
        connector = {
            "id": "sharepoint", "name": "SharePoint", "status": status,
            "configured": False, "tier": "standard", "metrics": [],
            "lastSynced": "—", "reads": [], "signalStrength": 0, "category": "Docs",
        }
    else:
        connector = {**connector, "status": status}
    connector.pop("sites", None)
    db.org_connector_set("default", "sharepoint", connector)


class TestGetSharePointSites:
    def test_routes_registered(self):
        paths = {(r.path, m) for r in app.routes for m in getattr(r, "methods", set())}
        assert ("/api/connectors/sharepoint/sites", "GET") in paths
        assert ("/api/connectors/sharepoint/sites", "PATCH") in paths

    def test_get_lists_sites(self):
        _set_status("connected")
        resp = client.get("/api/connectors/sharepoint/sites", headers=auth())
        assert resp.status_code == 200
        data = resp.json()
        assert SITE in {s["id"] for s in data["available"]}
        assert data["configured"] is False
        assert data["selected"] == []

    def test_requires_auth(self):
        assert client.get("/api/connectors/sharepoint/sites").status_code == 401

    def test_get_degrades_to_empty_on_listing_failure(self, monkeypatch):
        _set_status("connected")

        def boom(_org_id=None):
            raise RuntimeError("Graph permission not granted")

        monkeypatch.setattr("discovery.ingest.sharepoint.list_selectable_sites", boom)
        resp = client.get("/api/connectors/sharepoint/sites", headers=auth())
        assert resp.status_code == 200
        assert resp.json()["available"] == []


class TestPatchSharePointSites:
    def test_stores_selection(self):
        _set_status("connected")
        resp = client.patch(
            "/api/connectors/sharepoint/sites", headers=auth(), json={"sites": [SITE]}
        )
        assert resp.status_code == 200
        assert resp.json()["selected"] == [SITE]
        assert resp.json()["configured"] is True
        got = client.get("/api/connectors/sharepoint/sites", headers=auth()).json()
        assert got["selected"] == [SITE]

    def test_filters_unknown(self):
        _set_status("connected")
        resp = client.patch(
            "/api/connectors/sharepoint/sites", headers=auth(),
            json={"sites": [SITE, "S-nope"]},
        )
        assert resp.json()["selected"] == [SITE]

    def test_empty_accepted(self):
        _set_status("connected")
        resp = client.patch(
            "/api/connectors/sharepoint/sites", headers=auth(), json={"sites": []}
        )
        assert resp.status_code == 200
        assert resp.json()["selected"] == []

    def test_400_when_not_connected(self):
        _set_status("disconnected")
        resp = client.patch(
            "/api/connectors/sharepoint/sites", headers=auth(), json={"sites": [SITE]}
        )
        assert resp.status_code == 400

    def test_upstream_failure_preserves_selection(self, monkeypatch):
        _set_status("connected")
        connector = db.org_connector_get("default", "sharepoint")
        connector["sites"] = [SITE]
        db.org_connector_set("default", "sharepoint", connector)

        def unavailable(_org_id=None):
            raise RuntimeError("temporary Graph outage")

        monkeypatch.setattr("discovery.ingest.sharepoint.list_selectable_sites", unavailable)
        resp = client.patch(
            "/api/connectors/sharepoint/sites", headers=auth(), json={"sites": []}
        )
        assert resp.status_code == 503
        assert db.org_connector_get("default", "sharepoint")["sites"] == [SITE]

    def test_requires_auth(self):
        assert client.patch(
            "/api/connectors/sharepoint/sites", json={"sites": [SITE]}
        ).status_code == 401


class TestSelectionNarrowsScope:
    def test_selected_site_ids_reflects_selection(self):
        _set_status("connected")
        connector = db.org_connector_get("default", "sharepoint")
        connector["sites"] = [SITE]
        db.org_connector_set("default", "sharepoint", connector)

        from discovery.ingest.sharepoint import SharePointIngestor
        assert SharePointIngestor()._selected_site_ids("default") == {SITE}

    def test_unmatched_selection_yields_no_libraries(self):
        _set_status("connected")
        connector = db.org_connector_get("default", "sharepoint")
        connector["sites"] = ["S-does-not-exist"]
        db.org_connector_set("default", "sharepoint", connector)

        from discovery.ingest.sharepoint import SharePointIngestor
        # A selection that matches no granted site → nothing accessible.
        assert SharePointIngestor()._accessible_libraries("default") == []

    def test_no_selection_reads_all_granted(self):
        _set_status("connected")  # clears selection
        from discovery.ingest.sharepoint import SharePointIngestor
        assert SharePointIngestor()._selected_site_ids("default") is None
