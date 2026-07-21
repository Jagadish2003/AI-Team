"""
test_confluence_spaces.py — Confluence multi-space selection contract tests.

Runs offline (INGEST_MODE unset → fixture), so the selectable spaces are
deterministic: ENG, OPS.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app import db

client = TestClient(app)


@pytest.fixture(autouse=True, scope="module")
def seed_dev_owner():
    from app.rbac import seed_owner, _ensure_members_table
    _ensure_members_table()
    seed_owner("default", "dev-token-change-me")


def auth():
    return {"Authorization": "Bearer dev-token-change-me"}


def _set_status(status: str):
    connector = db.org_connector_get("default", "confluence")
    if not connector:
        connector = {
            "id": "confluence", "name": "Confluence", "status": status,
            "configured": False, "tier": "standard", "metrics": [],
            "lastSynced": "—", "reads": [], "signalStrength": 0, "category": "Docs",
        }
    else:
        connector = {**connector, "status": status}
    connector.pop("spaces", None)
    db.org_connector_set("default", "confluence", connector)


class TestGetConfluenceSpaces:
    def test_routes_registered(self):
        paths = {(r.path, m) for r in app.routes for m in getattr(r, "methods", set())}
        assert ("/api/connectors/confluence/spaces", "GET") in paths
        assert ("/api/connectors/confluence/spaces", "PATCH") in paths

    def test_get_lists_spaces(self):
        _set_status("connected")
        resp = client.get("/api/connectors/confluence/spaces", headers=auth())
        assert resp.status_code == 200
        data = resp.json()
        assert {"ENG", "OPS"} <= {s["key"] for s in data["available"]}
        assert data["configured"] is False
        assert data["selected"] == []

    def test_requires_auth(self):
        assert client.get("/api/connectors/confluence/spaces").status_code == 401

    def test_get_degrades_to_empty_on_listing_failure(self, monkeypatch):
        # A listing failure (e.g. Confluence auth not ready) must NOT 503 the GET —
        # it degrades to an empty option list so the panel still renders.
        _set_status("connected")

        def boom(_org_id=None):
            raise RuntimeError("Confluence auth not ready")

        monkeypatch.setattr("discovery.ingest.confluence.list_selectable_spaces", boom)
        resp = client.get("/api/connectors/confluence/spaces", headers=auth())
        assert resp.status_code == 200
        assert resp.json()["available"] == []


class TestPatchConfluenceSpaces:
    def test_stores_multiple(self):
        _set_status("connected")
        resp = client.patch(
            "/api/connectors/confluence/spaces", headers=auth(),
            json={"spaces": ["ENG", "OPS"]},
        )
        assert resp.status_code == 200
        assert resp.json()["selected"] == ["ENG", "OPS"]
        assert resp.json()["configured"] is True
        got = client.get("/api/connectors/confluence/spaces", headers=auth()).json()
        assert got["selected"] == ["ENG", "OPS"]

    def test_filters_unknown(self):
        _set_status("connected")
        resp = client.patch(
            "/api/connectors/confluence/spaces", headers=auth(),
            json={"spaces": ["ENG", "NOPE"]},
        )
        assert resp.json()["selected"] == ["ENG"]

    def test_empty_accepted(self):
        _set_status("connected")
        resp = client.patch(
            "/api/connectors/confluence/spaces", headers=auth(), json={"spaces": []}
        )
        assert resp.status_code == 200
        assert resp.json()["selected"] == []

    def test_400_when_not_connected(self):
        _set_status("disconnected")
        resp = client.patch(
            "/api/connectors/confluence/spaces", headers=auth(), json={"spaces": ["ENG"]}
        )
        assert resp.status_code == 400

    def test_upstream_failure_preserves_selection(self, monkeypatch):
        _set_status("connected")
        connector = db.org_connector_get("default", "confluence")
        connector["spaces"] = ["ENG"]
        db.org_connector_set("default", "confluence", connector)

        def unavailable(_org_id=None):
            raise RuntimeError("temporary Confluence outage")

        monkeypatch.setattr("discovery.ingest.confluence.list_selectable_spaces", unavailable)
        resp = client.patch(
            "/api/connectors/confluence/spaces", headers=auth(), json={"spaces": ["OPS"]}
        )
        assert resp.status_code == 503
        assert db.org_connector_get("default", "confluence")["spaces"] == ["ENG"]

    def test_requires_auth(self):
        assert client.patch(
            "/api/connectors/confluence/spaces", json={"spaces": ["ENG"]}
        ).status_code == 401


class TestSelectionNarrowsScope:
    def test_selection_narrows_accessible_spaces(self):
        _set_status("connected")
        connector = db.org_connector_get("default", "confluence")
        connector["spaces"] = ["ENG"]
        db.org_connector_set("default", "confluence", connector)

        from discovery.ingest.confluence import ConfluenceIngestor
        spaces = ConfluenceIngestor()._accessible_spaces("default")
        assert {s["key"] for s in spaces} == {"ENG"}

    def test_no_selection_reads_all_granted(self):
        _set_status("connected")  # clears the selection
        from discovery.ingest.confluence import ConfluenceIngestor
        spaces = ConfluenceIngestor()._accessible_spaces("default")
        assert {"ENG", "OPS"} <= {s["key"] for s in spaces}
