"""2.0-D4 T1 / AC1 — the D4-named actions write real audit records.

The conformance sweep in ``test_audit_conformance.py`` proves a route *can reach*
``log_event`` by walking the call graph. That is necessary and not sufficient: "the
handler calls log_event" and "an audit record exists with the required fields" are
different claims, and only the second is what a security review asks for.

So this file drives the routes D4 names by hand and reads the stored row back,
checking the five required fields — actor, org, target, timestamp, outcome.

Scope pin/unpin gets the most attention because it was the largest hole: seven
routes persisted a data-access grant with nothing recorded, while a registry-level
check passed because the cloud and native-DB paths emitted the same event type.
"""

from __future__ import annotations

import json
import os
from contextlib import closing
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app
from app.middleware import audit

DEV_TOKEN = os.getenv("DEV_JWT", "dev-token-change-me")


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


def _org() -> str:
    from app.rbac import _ensure_members_table

    org_id = f"org-d4t1-{uuid4().hex[:8]}"
    _ensure_members_table()
    with closing(db.connect()) as con:
        with con.cursor() as cur:
            cur.execute(
                "INSERT INTO workspace_members (org_id, user_id, role, created_at) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                (org_id, DEV_TOKEN, "owner", datetime.now(timezone.utc).isoformat()),
            )
        con.commit()
    return org_id


def _auth(org_id: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {DEV_TOKEN}", "X-Org-Id": org_id}


def _rows(org_id: str, event_type: str) -> List[Dict[str, Any]]:
    audit._ensure_table()
    with closing(db.connect()) as con:
        with con.cursor() as cur:
            cur.execute(
                "SELECT user_id, connector_id, run_id, payload, timestamp "
                "FROM audit_log WHERE org_id = %s AND event_type = %s "
                "ORDER BY timestamp DESC",
                (org_id, event_type),
            )
            out = []
            for user_id, connector_id, run_id, payload, ts in cur.fetchall():
                if isinstance(payload, str):
                    payload = json.loads(payload)
                out.append({
                    "user_id": user_id,
                    "connector_id": connector_id,
                    "run_id": run_id,
                    "payload": payload or {},
                    "timestamp": ts,
                })
            return out


def _seed_connector(org_id: str, connector_id: str, **extra: Any) -> None:
    record = {
        "id": connector_id,
        "name": connector_id,
        "status": "connected",
        "tier": "recommended",
        "lastSynced": "-",
    }
    record.update(extra)
    db.org_connector_set(org_id, connector_id, record)


def _assert_required_fields(row: Dict[str, Any], *, org_id: str) -> None:
    """D4's five: actor, org, target, timestamp, outcome.

    The org is asserted by construction — the query is org-scoped, so a row that
    came back at all was filed under this tenant rather than the shared default.
    """
    assert row["user_id"], "no actor recorded"
    assert row["timestamp"], "no timestamp recorded"
    payload = row["payload"]
    assert payload.get("target"), "no target recorded"
    assert payload.get("outcome") in audit.OUTCOME_VALUES, (
        f"outcome missing or not a known value: {payload.get('outcome')!r}"
    )


# ---------------------------------------------------------------------------
# Scope pin / unpin — the largest hole D4 named
# ---------------------------------------------------------------------------


SCOPE_CASES = [
    # connector, route, body key, initial selection, new selection, scope key
    ("slack", "/api/connectors/slack/channels", "channels",
     ["C1", "C2"], ["C2", "C3"], "channels"),
    ("teams", "/api/connectors/teams/channels", "channels",
     ["T1"], ["T1", "T2"], "channels"),
    ("confluence", "/api/connectors/confluence/spaces", "spaces",
     ["ENG"], ["OPS"], "spaces"),
    ("sharepoint", "/api/connectors/sharepoint/sites", "sites",
     ["S1"], [], "sites"),
    ("github", "/api/connectors/github/repos", "repos",
     ["r/one"], ["r/one", "r/two"], "repos"),
]


class TestScopeSelectionIsRecorded:
    def test_a_scope_change_writes_an_audit_row(self, client, monkeypatch):
        """The core AC1 case for scope pin/unpin.

        A scope selection decides what AgentIQ may read for this org, for every
        future run. Widening it is a data-access grant, and before this it left
        no trace at all.
        """
        org = _org()
        _seed_connector(org, "slack", channels=["C1", "C2"])
        monkeypatch.setattr(
            "app.routes_slack_channels._selectable_channels",
            lambda _org: [{"id": c, "name": c} for c in ("C1", "C2", "C3")],
        )
        response = client.patch(
            "/api/connectors/slack/channels",
            json={"channels": ["C2", "C3"]},
            headers=_auth(org),
        )
        assert response.status_code == 200, response.text

        rows = _rows(org, audit.SCOPE_DECLARED)
        assert rows, "a scope change wrote no audit row"
        _assert_required_fields(rows[0], org_id=org)

    def test_the_row_names_what_was_pinned_and_unpinned(self, client, monkeypatch):
        """Recording only the resulting selection forces a reviewer to diff
        consecutive rows, and cannot answer the question at all for the first
        write. 'Who added #finance?' needs the delta."""
        org = _org()
        _seed_connector(org, "slack", channels=["C1", "C2"])
        monkeypatch.setattr(
            "app.routes_slack_channels._selectable_channels",
            lambda _org: [{"id": c, "name": c} for c in ("C1", "C2", "C3")],
        )
        client.patch(
            "/api/connectors/slack/channels",
            json={"channels": ["C2", "C3"]},
            headers=_auth(org),
        )
        payload = _rows(org, audit.SCOPE_DECLARED)[0]["payload"]
        assert payload["pinned"] == ["C3"]
        assert payload["unpinned"] == ["C1"]
        assert payload["selected"] == ["C2", "C3"]
        assert payload["scope_key"] == "channels"

    def test_clearing_a_scope_entirely_is_still_recorded(self, client, monkeypatch):
        """Removing every id is the change most worth having a record of, and is
        the one an implementation keyed on 'is there a selection?' would drop."""
        org = _org()
        _seed_connector(org, "sharepoint", sites=["S1"])
        monkeypatch.setattr(
            "app.routes_sharepoint_sites._selectable_sites",
            lambda _org: [{"id": "S1", "name": "S1"}],
        )
        client.patch(
            "/api/connectors/sharepoint/sites",
            json={"sites": []},
            headers=_auth(org),
        )
        rows = _rows(org, audit.SCOPE_DECLARED)
        assert rows, "clearing a scope wrote no audit row"
        assert rows[0]["payload"]["unpinned"] == ["S1"]
        assert rows[0]["payload"]["selected_count"] == 0

    def test_the_first_selection_is_marked_as_such(self, client, monkeypatch):
        """Before a selection exists most connectors read everything they can
        see, so the first save is usually a NARROWING even though every id shows
        as pinned. Without the flag that row reads as a broad grant."""
        org = _org()
        _seed_connector(org, "github")  # no repos key at all
        monkeypatch.setattr(
            "app.routes_github_repos._selectable_repos",
            lambda _org: [{"id": "r/one", "name": "one"}],
        )
        client.patch(
            "/api/connectors/github/repos",
            json={"repos": ["r/one"]},
            headers=_auth(org),
        )
        rows = _rows(org, audit.SCOPE_DECLARED)
        assert rows and rows[0]["payload"]["first_selection"] is True

    def test_the_connector_is_identified_on_the_row(self, client, monkeypatch):
        """Seven connectors share one event type, so a row that does not name its
        connector is unattributable."""
        org = _org()
        _seed_connector(org, "slack", channels=[])
        monkeypatch.setattr(
            "app.routes_slack_channels._selectable_channels",
            lambda _org: [{"id": "C1", "name": "C1"}],
        )
        client.patch(
            "/api/connectors/slack/channels",
            json={"channels": ["C1"]},
            headers=_auth(org),
        )
        row = _rows(org, audit.SCOPE_DECLARED)[0]
        assert row["connector_id"] == "slack"
        assert row["payload"]["target"] == "slack:channels"


class TestScopeDeltaLogic:
    """The delta is pure, so its edges are worth pinning directly."""

    def test_pinned_and_unpinned_are_computed_in_stable_order(self):
        from app.connector_scope_audit import scope_delta

        pinned, unpinned = scope_delta(["a", "b", "c"], ["c", "d", "e"])
        assert pinned == ["d", "e"]
        assert unpinned == ["a", "b"]

    def test_an_absent_previous_selection_is_all_pinned(self):
        from app.connector_scope_audit import scope_delta

        assert scope_delta(None, ["a"]) == (["a"], [])

    def test_a_selection_of_objects_resolves_to_ids(self):
        """Some connector records store richer objects than bare ids; a scope
        audit that silently produced an empty delta for those would be worse
        than none, because it would look like nothing changed."""
        from app.connector_scope_audit import scope_delta

        pinned, unpinned = scope_delta([{"id": "a"}], [{"id": "b"}])
        assert pinned == ["b"] and unpinned == ["a"]

    def test_an_unchanged_selection_records_no_delta(self):
        from app.connector_scope_audit import scope_delta

        assert scope_delta(["a"], ["a"]) == ([], [])


# ---------------------------------------------------------------------------
# Connector create / edit
# ---------------------------------------------------------------------------


class TestConnectorLifecycleIsRecorded:
    def test_connecting_writes_a_connector_connected_row(self, client):
        org = _org()
        _seed_connector(org, "jira", status="disconnected")
        response = client.post(
            "/api/connectors/jira/connect",
            json={"status": "connected"},
            headers=_auth(org),
        )
        assert response.status_code == 200, response.text
        rows = _rows(org, audit.CONNECTOR_CONNECTED)
        assert rows, "connecting wrote no audit row"
        _assert_required_fields(rows[0], org_id=org)
        assert rows[0]["connector_id"] == "jira"

    def test_the_same_route_disconnecting_reads_as_a_disconnect(self, client):
        """The event follows the resulting state rather than the route name, so
        a toggle to a non-connected status matches what the OAuth revoke path
        emits for the same outcome."""
        org = _org()
        _seed_connector(org, "jira", status="connected")
        client.post(
            "/api/connectors/jira/connect",
            json={"status": "disconnected"},
            headers=_auth(org),
        )
        assert _rows(org, audit.CONNECTOR_DISCONNECTED)
        assert not _rows(org, audit.CONNECTOR_CONNECTED)

    def test_configuring_writes_a_row_naming_the_settings_changed(self, client):
        org = _org()
        _seed_connector(org, "servicenow", status="connected")
        response = client.post(
            "/api/connectors/servicenow/configure",
            json={"cmdb_class_scope": ["cmdb_ci_server"]},
            headers=_auth(org),
        )
        assert response.status_code == 200, response.text
        rows = _rows(org, audit.CONNECTOR_CONFIGURED)
        assert rows, "configuring wrote no audit row"
        _assert_required_fields(rows[0], org_id=org)
        assert "cmdb_class_scope" in rows[0]["payload"]["settings_changed"]

    def test_the_configuration_values_are_not_copied_into_the_trail(self, client):
        """The body can carry customer configuration. The row records WHICH
        settings changed, never their contents — an audit row is not a place to
        duplicate customer data."""
        org = _org()
        _seed_connector(org, "servicenow", status="connected")
        client.post(
            "/api/connectors/servicenow/configure",
            json={"cmdb_class_scope": ["cmdb_ci_server"]},
            headers=_auth(org),
        )
        serialised = json.dumps(_rows(org, audit.CONNECTOR_CONFIGURED)[0]["payload"])
        assert "cmdb_ci_server" not in serialised


# ---------------------------------------------------------------------------
# Run start
# ---------------------------------------------------------------------------


class TestRunStartIsRecorded:
    def test_launching_from_the_stack_builder_writes_a_run_started_row(self, client):
        """The path the product actually uses. run_started was emitted by the
        other run route only, so a Stack Builder launch left no audit row."""
        org = _org()
        response = client.post(
            "/api/stack-builder/launch",
            json={
                "org_id": org,
                "pack_id": "service_cloud",
                "selected_system_ids": ["salesforce", "servicenow"],
            },
            headers=_auth(org),
        )
        assert response.status_code == 200, response.text
        run_id = response.json()["runId"]

        rows = _rows(org, audit.RUN_STARTED)
        assert rows, "a Stack Builder launch wrote no audit row"
        _assert_required_fields(rows[0], org_id=org)
        assert rows[0]["run_id"] == run_id

    def test_the_row_records_what_the_run_was_scoped_to(self, client):
        """'Which sources did this run read?' is the question a reviewer follows
        a run start with, so the answer travels on the row."""
        org = _org()
        client.post(
            "/api/stack-builder/launch",
            json={
                "org_id": org,
                "pack_id": "service_cloud",
                "selected_system_ids": ["salesforce", "servicenow"],
            },
            headers=_auth(org),
        )
        payload = _rows(org, audit.RUN_STARTED)[0]["payload"]
        assert payload["pack_ids"] == ["service_cloud"]
        assert sorted(payload["systems"]) == ["salesforce", "servicenow"]
        assert payload["system_count"] == 2
        assert payload["source"] == "stack_builder"


# ---------------------------------------------------------------------------
# Cross-cutting
# ---------------------------------------------------------------------------


class TestTheTrailIsTenantScoped:
    def test_one_orgs_action_does_not_appear_in_anothers_trail(self, client, monkeypatch):
        """Isolation asserted after retrieval is not isolation. The audit trail
        is the artifact a customer hands an auditor, so a leak here is a leak of
        another tenant's activity."""
        org_a, org_b = _org(), _org()
        _seed_connector(org_a, "slack", channels=[])
        monkeypatch.setattr(
            "app.routes_slack_channels._selectable_channels",
            lambda _org: [{"id": "C1", "name": "C1"}],
        )
        client.patch(
            "/api/connectors/slack/channels",
            json={"channels": ["C1"]},
            headers=_auth(org_a),
        )
        assert _rows(org_a, audit.SCOPE_DECLARED)
        assert not _rows(org_b, audit.SCOPE_DECLARED)


class TestAnAuditFailureDoesNotBreakTheAction:
    def test_a_scope_change_still_succeeds_if_the_audit_write_fails(
        self, client, monkeypatch
    ):
        """The module's standing posture, re-verified at a new call site: the
        state change is already committed, so refusing the response would report
        a failure that did not happen.
        """
        org = _org()
        _seed_connector(org, "slack", channels=[])
        monkeypatch.setattr(
            "app.routes_slack_channels._selectable_channels",
            lambda _org: [{"id": "C1", "name": "C1"}],
        )
        monkeypatch.setattr(
            "app.connector_scope_audit.log_event",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("audit down")),
        )
        response = client.patch(
            "/api/connectors/slack/channels",
            json={"channels": ["C1"]},
            headers=_auth(org),
        )
        assert response.status_code == 200
        assert db.org_connector_get(org, "slack")["channels"] == ["C1"]
