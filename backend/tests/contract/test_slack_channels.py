"""
test_slack_channels.py — R18-C0 P5
Contract tests for the Slack channel-selection endpoints.

  GET /api/connectors/slack/channels:
    - 200 returns selectable channels (from the offline fixture: C001, C002),
      the saved selection, and the configured flag
    - excludes private / not-member / archived channels (C900/C901/C902)

  PATCH /api/connectors/slack/channels:
    - 200 stores the selection; unknown ids filtered; configured becomes True
    - empty list accepted (read nothing)
    - 400 when Slack not connected
    - 401 without auth

Runs offline (INGEST_MODE unset → fixture), so the selectable channels are
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


def _set_slack_status(status: str):
    org_id = "default"
    connector = db.org_connector_get(org_id, "slack")
    if not connector:
        connector = {
            "id": "slack", "name": "Slack", "status": status, "configured": False,
            "tier": "standard", "metrics": [], "lastSynced": "—",
            "reads": [], "signalStrength": 0, "category": "Comms · Ops",
        }
    else:
        connector = {**connector, "status": status}
    connector.pop("channels", None)  # reset selection between tests
    db.org_connector_set(org_id, "slack", connector)


class TestGetSlackChannels:
    def test_routes_are_registered_on_the_application(self):
        paths = {
            (route.path, method)
            for route in app.routes
            for method in getattr(route, "methods", set())
        }
        assert ("/api/connectors/slack/channels", "GET") in paths
        assert ("/api/connectors/slack/channels", "PATCH") in paths

    def test_get_lists_accessible_channels(self):
        _set_slack_status("connected")
        resp = client.get("/api/connectors/slack/channels", headers=auth())
        assert resp.status_code == 200
        data = resp.json()
        ids = {c["id"] for c in data["available"]}
        assert {"C001", "C002"} <= ids
        # private / not-member / archived are never selectable
        assert not ({"C900", "C901", "C902"} & ids)
        assert data["configured"] is False
        assert data["selected"] == []

    def test_requires_auth(self):
        resp = client.get("/api/connectors/slack/channels")
        assert resp.status_code == 401


class TestPatchSlackChannels:
    def test_patch_stores_selection(self):
        _set_slack_status("connected")
        resp = client.patch(
            "/api/connectors/slack/channels",
            headers=auth(),
            json={"channels": ["C001"]},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["selected"] == ["C001"]
        assert data["configured"] is True
        # Persisted: a follow-up GET reflects it.
        got = client.get("/api/connectors/slack/channels", headers=auth()).json()
        assert got["selected"] == ["C001"]
        assert got["configured"] is True

    def test_patch_filters_unknown_ids(self):
        _set_slack_status("connected")
        resp = client.patch(
            "/api/connectors/slack/channels",
            headers=auth(),
            json={"channels": ["C001", "C900", "does-not-exist"]},
        )
        assert resp.status_code == 200
        # C900 (private) and the bogus id are filtered out.
        assert resp.json()["selected"] == ["C001"]

    def test_patch_empty_list_accepted(self):
        _set_slack_status("connected")
        resp = client.patch(
            "/api/connectors/slack/channels",
            headers=auth(),
            json={"channels": []},
        )
        assert resp.status_code == 200
        assert resp.json()["selected"] == []
        assert resp.json()["configured"] is True

    def test_upstream_failure_preserves_saved_selection(self, monkeypatch):
        _set_slack_status("connected")
        connector = db.org_connector_get("default", "slack")
        connector["channels"] = ["C001"]
        db.org_connector_set("default", "slack", connector)

        def unavailable(_org_id):
            raise RuntimeError("temporary Slack API outage")

        monkeypatch.setattr(
            "discovery.ingest.slack.list_selectable_channels",
            unavailable,
        )

        resp = client.patch(
            "/api/connectors/slack/channels",
            headers=auth(),
            json={"channels": ["C002"]},
        )

        assert resp.status_code == 503
        assert "not changed" in resp.json()["detail"]
        persisted = db.org_connector_get("default", "slack")
        assert persisted["channels"] == ["C001"]

    def test_patch_400_when_not_connected(self):
        _set_slack_status("disconnected")
        resp = client.patch(
            "/api/connectors/slack/channels",
            headers=auth(),
            json={"channels": ["C001"]},
        )
        assert resp.status_code == 400

    def test_requires_auth(self):
        resp = client.patch(
            "/api/connectors/slack/channels",
            json={"channels": ["C001"]},
        )
        assert resp.status_code == 401
