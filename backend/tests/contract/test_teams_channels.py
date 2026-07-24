"""
test_teams_channels.py — Microsoft Teams channel-selection contract tests.

  GET /api/connectors/teams/channels:
    - 200 returns selectable channels (from the offline fixture: T-eng/19:ops,
      T-eng/19:deploys), the saved selection, and the configured flag
    - excludes private / not-granted / archived channels

  PATCH /api/connectors/teams/channels:
    - 200 stores the selection; unknown ids filtered; configured becomes True
    - empty list accepted (read nothing)
    - 400 when Teams not connected
    - 401 without auth
    - 503 on an upstream listing failure — saved selection preserved

Plus the selection narrows the ingestor's granted scope (reach + depth).

Runs offline (INGEST_MODE unset → fixture), so the selectable channels are
deterministic.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app import db

client = TestClient(app)

OPS = "T-eng/19:ops"
DEPLOYS = "T-eng/19:deploys"


@pytest.fixture(autouse=True, scope="module")
def seed_dev_owner():
    from app.rbac import seed_owner, _ensure_members_table
    _ensure_members_table()
    seed_owner("default", "dev-token-change-me")


def auth():
    return {"Authorization": "Bearer dev-token-change-me"}


def _set_teams_status(status: str):
    org_id = "default"
    connector = db.org_connector_get(org_id, "teams")
    if not connector:
        connector = {
            "id": "teams", "name": "Microsoft Teams", "status": status, "configured": False,
            "tier": "standard", "metrics": [], "lastSynced": "—",
            "reads": [], "signalStrength": 0, "category": "Comms / docs",
        }
    else:
        connector = {**connector, "status": status}
    connector.pop("channels", None)  # reset selection between tests
    db.org_connector_set(org_id, "teams", connector)


class TestGetTeamsChannels:
    def test_routes_are_registered(self):
        paths = {
            (route.path, method)
            for route in app.routes
            for method in getattr(route, "methods", set())
        }
        assert ("/api/connectors/teams/channels", "GET") in paths
        assert ("/api/connectors/teams/channels", "PATCH") in paths

    def test_get_lists_granted_channels(self):
        _set_teams_status("connected")
        resp = client.get("/api/connectors/teams/channels", headers=auth())
        assert resp.status_code == 200
        data = resp.json()
        ids = {c["id"] for c in data["available"]}
        assert {OPS, DEPLOYS} <= ids
        # private / not-granted / archived channels are never selectable
        assert not any("leads-private" in i or "not-granted" in i or "archived" in i for i in ids)
        assert data["configured"] is False
        assert data["selected"] == []

    def test_requires_auth(self):
        resp = client.get("/api/connectors/teams/channels")
        assert resp.status_code == 401


class TestPatchTeamsChannels:
    def test_patch_stores_selection(self):
        _set_teams_status("connected")
        resp = client.patch(
            "/api/connectors/teams/channels", headers=auth(), json={"channels": [OPS]}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["selected"] == [OPS]
        assert data["configured"] is True
        got = client.get("/api/connectors/teams/channels", headers=auth()).json()
        assert got["selected"] == [OPS]
        assert got["configured"] is True

    def test_patch_filters_unknown_ids(self):
        _set_teams_status("connected")
        resp = client.patch(
            "/api/connectors/teams/channels",
            headers=auth(),
            json={"channels": [OPS, "T-eng/19:leads-private", "does-not-exist"]},
        )
        assert resp.status_code == 200
        assert resp.json()["selected"] == [OPS]

    def test_patch_empty_list_accepted(self):
        _set_teams_status("connected")
        resp = client.patch(
            "/api/connectors/teams/channels", headers=auth(), json={"channels": []}
        )
        assert resp.status_code == 200
        assert resp.json()["selected"] == []
        assert resp.json()["configured"] is True

    def test_upstream_failure_preserves_saved_selection(self, monkeypatch):
        _set_teams_status("connected")
        connector = db.org_connector_get("default", "teams")
        connector["channels"] = [OPS]
        db.org_connector_set("default", "teams", connector)

        def unavailable(_org_id):
            raise RuntimeError("temporary Graph API outage")

        monkeypatch.setattr("discovery.ingest.teams.list_selectable_channels", unavailable)

        resp = client.patch(
            "/api/connectors/teams/channels", headers=auth(), json={"channels": [DEPLOYS]}
        )
        assert resp.status_code == 503
        assert "not changed" in resp.json()["detail"]
        assert db.org_connector_get("default", "teams")["channels"] == [OPS]

    def test_patch_400_when_not_connected(self):
        _set_teams_status("disconnected")
        resp = client.patch(
            "/api/connectors/teams/channels", headers=auth(), json={"channels": [OPS]}
        )
        assert resp.status_code == 400

    def test_requires_auth(self):
        resp = client.patch("/api/connectors/teams/channels", json={"channels": [OPS]})
        assert resp.status_code == 401


class TestSelectionNarrowsScope:
    def test_no_selection_reads_all_granted(self):
        _set_teams_status("connected")  # clears the selection
        from discovery.ingest.teams import TeamsIngestor
        scope = TeamsIngestor()._granted_scope_container_ids("default")
        assert scope == {OPS, DEPLOYS}

    def test_selection_narrows_reach_and_depth(self):
        _set_teams_status("connected")
        connector = db.org_connector_get("default", "teams")
        connector["channels"] = [OPS]
        db.org_connector_set("default", "teams", connector)

        from discovery.ingest.teams import TeamsIngestor
        ing = TeamsIngestor()
        # Depth scope narrowed to the selection …
        assert ing._granted_scope_container_ids("default") == {OPS}
        # … and the reach channel list too.
        reach_ids = {
            f"{c['team_id']}/{c['id']}" for c in ing._selected_accessible_channels("default")
        }
        assert reach_ids == {OPS}
