"""
test_jira_projects.py — Jira multi-project selection contract tests.

  GET /api/connectors/jira/projects:
    - 200 returns selectable projects (from the offline fixture: CRM, OPS, PLAT),
      the saved selection (list), and the configured flag
    - 401 without auth

  PATCH /api/connectors/jira/projects:
    - 200 stores multiple projects; configured becomes True; persisted
    - unknown keys filtered silently
    - empty list clears the selection (configured False)
    - 400 when Jira not connected
    - 503 on an upstream listing failure — saved selection preserved
    - 401 without auth

Plus resolve_jira_projects / resolve_jira_project honour the saved selection and
fall back to JIRA_PROJECT_KEY otherwise.

Runs offline (INGEST_MODE unset → fixture), so the selectable projects are
deterministic.
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


def _set_jira_status(status: str):
    org_id = "default"
    connector = db.org_connector_get(org_id, "jira")
    if not connector:
        connector = {
            "id": "jira", "name": "Jira", "status": status, "configured": False,
            "tier": "standard", "metrics": [], "lastSynced": "—",
            "reads": [], "signalStrength": 0, "category": "Issues / backlog",
        }
    else:
        connector = {**connector, "status": status}
    connector.pop("project", None)   # reset legacy + new selection between tests
    connector.pop("projects", None)
    db.org_connector_set(org_id, "jira", connector)


class TestGetJiraProjects:
    def test_routes_are_registered(self):
        paths = {
            (route.path, method)
            for route in app.routes
            for method in getattr(route, "methods", set())
        }
        assert ("/api/connectors/jira/projects", "GET") in paths
        assert ("/api/connectors/jira/projects", "PATCH") in paths

    def test_get_lists_projects(self):
        _set_jira_status("connected")
        resp = client.get("/api/connectors/jira/projects", headers=auth())
        assert resp.status_code == 200
        data = resp.json()
        keys = {p["key"] for p in data["available"]}
        assert {"CRM", "OPS", "PLAT"} <= keys
        assert data["configured"] is False
        assert data["selected"] == []

    def test_requires_auth(self):
        resp = client.get("/api/connectors/jira/projects")
        assert resp.status_code == 401

    def test_get_degrades_to_empty_on_listing_failure(self, monkeypatch):
        _set_jira_status("connected")

        def boom(_org_id=None):
            raise RuntimeError("Jira auth not ready")

        monkeypatch.setattr("discovery.ingest.jira.list_selectable_projects", boom)
        resp = client.get("/api/connectors/jira/projects", headers=auth())
        assert resp.status_code == 200
        assert resp.json()["available"] == []


class TestPatchJiraProjects:
    def test_patch_stores_multiple_projects(self):
        _set_jira_status("connected")
        resp = client.patch(
            "/api/connectors/jira/projects", headers=auth(),
            json={"projects": ["CRM", "OPS"]},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["selected"] == ["CRM", "OPS"]
        assert data["configured"] is True
        # Persisted: a follow-up GET reflects it.
        got = client.get("/api/connectors/jira/projects", headers=auth()).json()
        assert got["selected"] == ["CRM", "OPS"]
        assert got["configured"] is True

    def test_patch_filters_unknown_keys(self):
        _set_jira_status("connected")
        resp = client.patch(
            "/api/connectors/jira/projects", headers=auth(),
            json={"projects": ["CRM", "NOPE", "PLAT"]},
        )
        assert resp.status_code == 200
        assert resp.json()["selected"] == ["CRM", "PLAT"]

    def test_patch_empty_list_clears_selection(self):
        _set_jira_status("connected")
        client.patch(
            "/api/connectors/jira/projects", headers=auth(), json={"projects": ["OPS"]}
        )
        resp = client.patch(
            "/api/connectors/jira/projects", headers=auth(), json={"projects": []}
        )
        assert resp.status_code == 200
        assert resp.json()["selected"] == []
        assert resp.json()["configured"] is False

    def test_patch_400_when_not_connected(self):
        _set_jira_status("disconnected")
        resp = client.patch(
            "/api/connectors/jira/projects", headers=auth(), json={"projects": ["CRM"]}
        )
        assert resp.status_code == 400

    def test_upstream_failure_preserves_saved_selection(self, monkeypatch):
        _set_jira_status("connected")
        connector = db.org_connector_get("default", "jira")
        connector["projects"] = ["CRM"]
        db.org_connector_set("default", "jira", connector)

        def unavailable(_org_id=None):
            raise RuntimeError("temporary Jira API outage")

        monkeypatch.setattr("discovery.ingest.jira.list_selectable_projects", unavailable)

        resp = client.patch(
            "/api/connectors/jira/projects", headers=auth(), json={"projects": ["OPS"]}
        )
        assert resp.status_code == 503
        assert "not changed" in resp.json()["detail"]
        assert db.org_connector_get("default", "jira")["projects"] == ["CRM"]

    def test_requires_auth(self):
        resp = client.patch("/api/connectors/jira/projects", json={"projects": ["CRM"]})
        assert resp.status_code == 401


class TestResolveJiraProjects:
    def test_saved_selection_is_honoured(self):
        _set_jira_status("connected")
        connector = db.org_connector_get("default", "jira")
        connector["projects"] = ["PLAT", "OPS"]
        db.org_connector_set("default", "jira", connector)

        from discovery.ingest.jira import resolve_jira_projects, resolve_jira_project
        assert resolve_jira_projects("default") == ["PLAT", "OPS"]
        # Backward-compatible single accessor returns the first key.
        assert resolve_jira_project("default") == "PLAT"

    def test_legacy_single_project_still_honoured(self):
        _set_jira_status("connected")
        connector = db.org_connector_get("default", "jira")
        connector["project"] = "CRM"   # legacy single-project key
        db.org_connector_set("default", "jira", connector)

        from discovery.ingest.jira import resolve_jira_projects
        assert resolve_jira_projects("default") == ["CRM"]

    def test_falls_back_to_env_default_when_unset(self, monkeypatch):
        _set_jira_status("connected")  # clears the selection
        monkeypatch.delenv("JIRA_PROJECT_KEY", raising=False)
        from discovery.ingest.jira import resolve_jira_projects
        assert resolve_jira_projects("default") == ["AIC"]

    def test_env_override_used_when_no_selection(self, monkeypatch):
        _set_jira_status("connected")
        monkeypatch.setenv("JIRA_PROJECT_KEY", "ENGX,OPS")
        from discovery.ingest.jira import resolve_jira_projects
        assert resolve_jira_projects("default") == ["ENGX", "OPS"]
