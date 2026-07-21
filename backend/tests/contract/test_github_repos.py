"""
test_github_repos.py — GitHub multi-repo selection contract tests.

Runs offline (INGEST_MODE unset → fixture): selectable repos acme/web-app,
acme/api, acme/infra.
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
    connector = db.org_connector_get("default", "github")
    if not connector:
        connector = {
            "id": "github", "name": "GitHub", "status": status,
            "configured": False, "tier": "standard", "metrics": [],
            "lastSynced": "—", "reads": [], "signalStrength": 0, "category": "Engineering",
        }
    else:
        connector = {**connector, "status": status}
    connector.pop("repos", None)
    db.org_connector_set("default", "github", connector)


class TestGetGitHubRepos:
    def test_routes_registered(self):
        paths = {(r.path, m) for r in app.routes for m in getattr(r, "methods", set())}
        assert ("/api/connectors/github/repos", "GET") in paths
        assert ("/api/connectors/github/repos", "PATCH") in paths

    def test_get_lists_repos(self):
        _set_status("connected")
        resp = client.get("/api/connectors/github/repos", headers=auth())
        assert resp.status_code == 200
        data = resp.json()
        assert {"acme/web-app", "acme/api"} <= {r["id"] for r in data["available"]}
        assert data["configured"] is False
        assert data["selected"] == []

    def test_requires_auth(self):
        assert client.get("/api/connectors/github/repos").status_code == 401

    def test_get_degrades_to_empty_on_listing_failure(self, monkeypatch):
        _set_status("connected")

        def boom(_org_id=None):
            raise RuntimeError("GitHub token not ready")

        monkeypatch.setattr("connectors.saas.github.list_selectable_repos", boom)
        resp = client.get("/api/connectors/github/repos", headers=auth())
        assert resp.status_code == 200
        assert resp.json()["available"] == []


class TestPatchGitHubRepos:
    def test_stores_multiple(self):
        _set_status("connected")
        resp = client.patch(
            "/api/connectors/github/repos", headers=auth(),
            json={"repos": ["acme/web-app", "acme/api"]},
        )
        assert resp.status_code == 200
        assert resp.json()["selected"] == ["acme/web-app", "acme/api"]
        assert resp.json()["configured"] is True
        got = client.get("/api/connectors/github/repos", headers=auth()).json()
        assert got["selected"] == ["acme/web-app", "acme/api"]

    def test_filters_unknown(self):
        _set_status("connected")
        resp = client.patch(
            "/api/connectors/github/repos", headers=auth(),
            json={"repos": ["acme/api", "acme/ghost"]},
        )
        assert resp.json()["selected"] == ["acme/api"]

    def test_empty_accepted(self):
        _set_status("connected")
        resp = client.patch(
            "/api/connectors/github/repos", headers=auth(), json={"repos": []}
        )
        assert resp.status_code == 200
        assert resp.json()["selected"] == []

    def test_400_when_not_connected(self):
        _set_status("disconnected")
        resp = client.patch(
            "/api/connectors/github/repos", headers=auth(), json={"repos": ["acme/api"]}
        )
        assert resp.status_code == 400

    def test_upstream_failure_preserves_selection(self, monkeypatch):
        _set_status("connected")
        connector = db.org_connector_get("default", "github")
        connector["repos"] = ["acme/api"]
        db.org_connector_set("default", "github", connector)

        def unavailable(_org_id=None):
            raise RuntimeError("temporary GitHub outage")

        monkeypatch.setattr("connectors.saas.github.list_selectable_repos", unavailable)
        resp = client.patch(
            "/api/connectors/github/repos", headers=auth(), json={"repos": []}
        )
        assert resp.status_code == 503
        assert db.org_connector_get("default", "github")["repos"] == ["acme/api"]

    def test_requires_auth(self):
        assert client.patch(
            "/api/connectors/github/repos", json={"repos": ["acme/api"]}
        ).status_code == 401


class TestSelectionNarrowsScope:
    def test_selected_repos_reflects_selection(self):
        _set_status("connected")
        connector = db.org_connector_get("default", "github")
        connector["repos"] = ["acme/api", "acme/infra"]
        db.org_connector_set("default", "github", connector)

        from connectors.saas.github import _selected_repos
        assert _selected_repos("default") == [("acme", "api"), ("acme", "infra")]

    def test_no_selection_returns_empty(self):
        _set_status("connected")  # clears selection
        from connectors.saas.github import _selected_repos
        assert _selected_repos("default") == []
