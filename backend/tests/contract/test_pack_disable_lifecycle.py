"""2.0-C1 T2 (AT-827) — safe disable over the HTTP surface, end to end.

Parent-story criteria exercised here:

  * **AC2** — disabling a pack stops future execution while all historical findings
    remain retrievable and correctly labelled; re-enable is supported.
  * **AC4** (contributing) — disabling deletes nothing: the run record, its
    opportunities, and its evidence are all still there afterwards. AT-829 owns the
    exhaustive data-layer sweep.
  * **AC5** — run health reports pack state and version accurately across the
    active → disabled → active transitions.

The state machine itself is pinned DB-free in
``tests/unit/test_pack_state_machine.py``; this suite pins the API contract, the
RBAC boundary, and the run-scoped persistence.
"""
from __future__ import annotations

import os
from typing import Any, Dict, Iterator, List
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app
from app.pack_state import (
    DISABLED_PACK_LABEL,
    InMemoryPackStateStore,
    STATE_ACTIVE,
    STATE_DISABLED,
    set_pack_state_store,
)
from app.rbac import seed_owner

OWNER_TOKEN = os.getenv("DEV_JWT", "dev-token-change-me")
ANALYST_TOKEN = "analyst-token"
VIEWER_TOKEN = "viewer-token"

# The org requests are scoped to, or None to send NO X-Org-Id header — which is the
# default, so every test that already passes keeps its exact previous request shape
# (the static analyst/viewer tokens resolve their org and role as before). Only the
# run-health tests opt in to a throwaway org (see `isolated_org`).
_CURRENT_ORG: Dict[str, Any] = {"id": None}


def _owner_org(prefix: str) -> str:
    """A throwaway org with the dev token seeded as its owner."""
    org_id = f"{prefix}_{uuid4().hex[:8]}"
    seed_owner(org_id, OWNER_TOKEN)
    return org_id


@pytest.fixture
def isolated_org() -> Iterator[str]:
    """Scope one test to a fresh org.

    Required by anything asserting on `GET /api/run-health/packs`, because
    `health_aggregation._latest_run` returns the newest run **for the org across the
    whole contract database** — and that database is dropped only once per session.
    Other suites create runs in the shared `default` org, so a test that assumed "the
    run I just launched is the latest" passed alone and failed in a full run, which is
    exactly how these tests failed in CI.
    """
    previous = _CURRENT_ORG["id"]
    _CURRENT_ORG["id"] = _owner_org("pack_disable")
    try:
        yield _CURRENT_ORG["id"]
    finally:
        _CURRENT_ORG["id"] = previous


@pytest.fixture(autouse=True)
def _role_tokens(monkeypatch):
    """Make the static analyst/viewer tokens acceptable to require_auth."""
    monkeypatch.setenv("ANALYST_JWT", ANALYST_TOKEN)
    monkeypatch.setenv("VIEWER_JWT", VIEWER_TOKEN)
    yield


@pytest.fixture(autouse=True)
def _in_memory_pack_state():
    """Isolate pack state per test. The Postgres store is restored afterwards, so
    no test can leave a pack disabled for another."""
    set_pack_state_store(InMemoryPackStateStore())
    yield
    set_pack_state_store(None)


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def _auth(token: str = OWNER_TOKEN) -> Dict[str, str]:
    headers = {"Authorization": f"Bearer {token}"}
    # Only sent when a test opted into an isolated org, so the default request shape
    # is byte-identical to before.
    if _CURRENT_ORG["id"] is not None:
        headers["X-Org-Id"] = _CURRENT_ORG["id"]
    return headers


def _launch_body(**overrides: Any) -> Dict[str, Any]:
    """A launch payload, defaulting to a single `service_cloud` selection.

    When the caller supplies ``pack_ids`` the default singular ``pack_id`` is
    DROPPED. ``LaunchRequest`` reconciles the two into one selection, so leaving it
    in made ``pack_ids=["cloud_ops"]`` actually mean
    ``["cloud_ops", "service_cloud"]`` — which silently defeated the
    "only disabled packs" tests: a runnable pack was always in the selection, so the
    launch correctly returned 200 instead of the 409 the test was asserting.
    """
    body: Dict[str, Any] = {
        "org_id": "default",
        "selected_system_ids": ["salesforce", "servicenow"],
        "weightings": {},
    }
    if "pack_ids" not in overrides:
        body["pack_id"] = "service_cloud"
    body.update(overrides)
    return body


def _set_state(client, pack_id: str, state: str, **kwargs) -> Any:
    payload: Dict[str, Any] = {"state": state}
    payload.update(kwargs)
    return client.put(
        f"/api/packs/{pack_id}/state", json=payload, headers=_auth()
    )


# ── The state endpoints ───────────────────────────────────────────────────────


class TestPackStateEndpoints:
    def test_state_list_reports_every_pack_active_by_default(self, client):
        response = client.get("/api/packs/state", headers=_auth())
        assert response.status_code == 200, response.text
        packs = response.json()["packs"]
        assert packs, "expected the registry to be reported"
        assert all(row["state"] == STATE_ACTIVE for row in packs)

    def test_owner_can_disable_a_pack(self, client):
        response = _set_state(client, "cloud_ops", STATE_DISABLED, reason="opted out")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["state"] == STATE_DISABLED
        assert body["previousState"] == STATE_ACTIVE
        assert body["transition"] == "disable"
        assert body["changed"] is True
        assert body["reason"] == "opted out"

    def test_the_disable_is_reflected_in_the_state_list(self, client):
        _set_state(client, "cloud_ops", STATE_DISABLED)
        rows = {
            row["packId"]: row
            for row in client.get("/api/packs/state", headers=_auth()).json()["packs"]
        }
        assert rows["cloud_ops"]["state"] == STATE_DISABLED
        assert rows["service_cloud"]["state"] == STATE_ACTIVE

    def test_repeating_a_disable_is_idempotent(self, client):
        _set_state(client, "cloud_ops", STATE_DISABLED)
        again = _set_state(client, "cloud_ops", STATE_DISABLED)
        assert again.status_code == 200
        assert again.json()["changed"] is False

    def test_re_enable_is_supported(self, client):
        _set_state(client, "cloud_ops", STATE_DISABLED)
        response = _set_state(client, "cloud_ops", STATE_ACTIVE)
        assert response.status_code == 200, response.text
        assert response.json()["state"] == STATE_ACTIVE
        assert response.json()["transition"] == "enable"

    def test_unknown_pack_is_404(self, client):
        response = _set_state(client, "no_such_pack", STATE_DISABLED)
        assert response.status_code == 404

    def test_illegal_state_is_rejected(self, client):
        response = _set_state(client, "cloud_ops", "paused")
        assert response.status_code == 422

    def test_history_is_newest_first_and_keeps_the_disable(self, client):
        _set_state(client, "cloud_ops", STATE_DISABLED, reason="turning off")
        _set_state(client, "cloud_ops", STATE_ACTIVE, reason="turning back on")
        response = client.get(
            "/api/packs/cloud_ops/state/history", headers=_auth()
        )
        assert response.status_code == 200, response.text
        transitions = response.json()["transitions"]
        # Re-enabling does NOT erase the disable — that is the audit trail (AC4).
        assert [t["transition"] for t in transitions] == ["enable", "disable"]
        assert transitions[1]["reason"] == "turning off"

    def test_history_for_an_unknown_pack_is_404(self, client):
        response = client.get(
            "/api/packs/no_such_pack/state/history", headers=_auth()
        )
        assert response.status_code == 404


class TestPackStateRbac:
    def test_viewer_can_read_state(self, client):
        # A viewer seeing a "now-disabled pack" label must be able to confirm it.
        response = client.get("/api/packs/state", headers=_auth(VIEWER_TOKEN))
        assert response.status_code == 200, response.text

    def test_analyst_cannot_change_state(self, client):
        response = client.put(
            "/api/packs/cloud_ops/state",
            json={"state": STATE_DISABLED},
            headers=_auth(ANALYST_TOKEN),
        )
        assert response.status_code == 403, response.text

    def test_viewer_cannot_change_state(self, client):
        response = client.put(
            "/api/packs/cloud_ops/state",
            json={"state": STATE_DISABLED},
            headers=_auth(VIEWER_TOKEN),
        )
        assert response.status_code == 403, response.text

    def test_unauthenticated_is_rejected(self, client):
        assert client.get("/api/packs/state").status_code in (401, 403)


# ── AC2 — disabling stops FUTURE execution ────────────────────────────────────


class TestDisabledPackDoesNotRun:
    def test_launch_excludes_a_disabled_pack_and_names_it(self, client):
        _set_state(client, "cloud_ops", STATE_DISABLED)
        response = client.post(
            "/api/stack-builder/launch",
            json=_launch_body(pack_ids=["service_cloud", "cloud_ops"]),
            headers=_auth(),
        )
        assert response.status_code == 200, response.text
        body = response.json()
        # The run will execute only the enabled pack...
        assert body["packIds"] == ["service_cloud"]
        # ...and the exclusion is named, never silent.
        assert [row["packId"] for row in body["excludedPacks"]] == ["cloud_ops"]
        assert body["excludedPacks"][0]["reason"] == "pack_disabled"

    def test_the_run_record_reports_the_exclusion(self, client):
        _set_state(client, "cloud_ops", STATE_DISABLED)
        run_id = client.post(
            "/api/stack-builder/launch",
            json=_launch_body(pack_ids=["service_cloud", "cloud_ops"]),
            headers=_auth(),
        ).json()["runId"]

        run = db.run_get(run_id)
        assert run["packIds"] == ["service_cloud"]
        assert [row["packId"] for row in run["excludedPacks"]] == ["cloud_ops"]
        # The disabled pack must not appear in the compatibility snapshot either —
        # it is not part of this run at all.
        assert "cloud_ops" not in run["packCompatibility"]
        assert db.run_kv_get("pack_ids", run_id, []) == ["service_cloud"]

    def test_launching_only_disabled_packs_is_refused(self, client):
        _set_state(client, "cloud_ops", STATE_DISABLED)
        response = client.post(
            "/api/stack-builder/launch",
            json=_launch_body(pack_ids=["cloud_ops"]),
            headers=_auth(),
        )
        # A run with zero packs would report success having produced nothing.
        assert response.status_code == 409, response.text
        assert "cloud_ops" in response.json()["detail"]

    def test_all_disabled_launch_never_silently_falls_back(self, client):
        _set_state(client, "cloud_ops", STATE_DISABLED)
        response = client.post(
            "/api/stack-builder/launch",
            json=_launch_body(pack_ids=["cloud_ops"]),
            headers=_auth(),
        )
        assert response.status_code == 409
        # Explicitly NOT a 200 that quietly ran service_cloud instead.
        assert "runId" not in response.json()

    def test_compute_refuses_when_every_selected_pack_is_disabled(self, client):
        run_id = client.post(
            "/api/stack-builder/launch", json=_launch_body(), headers=_auth()
        ).json()["runId"]
        _set_state(client, "service_cloud", STATE_DISABLED)

        response = client.post(
            f"/api/runs/{run_id}/compute",
            json={"mode": "offline", "systems": ["salesforce"]},
            headers=_auth(),
        )
        assert response.status_code == 409, response.text

    def test_re_enabling_lets_the_pack_run_again(self, client):
        _set_state(client, "cloud_ops", STATE_DISABLED)
        _set_state(client, "cloud_ops", STATE_ACTIVE)
        response = client.post(
            "/api/stack-builder/launch",
            json=_launch_body(pack_ids=["service_cloud", "cloud_ops"]),
            headers=_auth(),
        )
        assert response.status_code == 200, response.text
        assert response.json()["packIds"] == ["service_cloud", "cloud_ops"]
        assert response.json()["excludedPacks"] == []

    def test_an_unaffected_pack_launches_normally(self, client):
        _set_state(client, "cloud_ops", STATE_DISABLED)
        response = client.post(
            "/api/stack-builder/launch",
            json=_launch_body(pack_id="service_cloud"),
            headers=_auth(),
        )
        assert response.status_code == 200, response.text
        assert response.json()["excludedPacks"] == []


# ── AC2 / AC4 — historical output survives and is labelled ────────────────────


class TestHistoricalFindingsSurviveADisable:
    def _seed_run_with_findings(self, client) -> str:
        """Launch a run and materialise two findings from two packs."""
        run_id = client.post(
            "/api/stack-builder/launch",
            json=_launch_body(pack_ids=["service_cloud", "cloud_ops"]),
            headers=_auth(),
        ).json()["runId"]

        db.run_kv_set(
            "opps",
            run_id,
            [
                {
                    "id": "opp-cloud",
                    "title": "Recurring resolution loop",
                    "packId": "cloud_ops",
                    "packVersion": "1.2.0",
                    "impact": 8.0,
                    "effort": 3.0,
                    "evidenceIds": ["ev-1", "ev-2"],
                    "decision": "UNREVIEWED",
                },
                {
                    "id": "opp-service",
                    "title": "Case repetition",
                    "packId": "service_cloud",
                    "packVersion": "1.0.0",
                    "impact": 6.0,
                    "effort": 2.0,
                    "evidenceIds": ["ev-3"],
                    "decision": "UNREVIEWED",
                },
            ],
        )
        return run_id

    def _findings(self, client, run_id: str) -> Dict[str, Dict[str, Any]]:
        response = client.get(
            f"/api/runs/{run_id}/opportunities", headers=_auth()
        )
        assert response.status_code == 200, response.text
        return {row["id"]: row for row in response.json()}

    def test_findings_are_still_retrievable_after_the_disable(self, client):
        run_id = self._seed_run_with_findings(client)
        _set_state(client, "cloud_ops", STATE_DISABLED)

        findings = self._findings(client, run_id)
        # BOTH findings are still served — nothing is filtered or removed.
        assert set(findings) == {"opp-cloud", "opp-service"}

    def test_the_disabled_packs_finding_is_clearly_marked(self, client):
        run_id = self._seed_run_with_findings(client)
        _set_state(client, "cloud_ops", STATE_DISABLED)

        findings = self._findings(client, run_id)
        assert findings["opp-cloud"]["packState"] == STATE_DISABLED
        assert findings["opp-cloud"]["packStateLabel"] == DISABLED_PACK_LABEL
        # The still-active pack's finding is NOT mislabelled.
        assert findings["opp-service"]["packState"] == STATE_ACTIVE
        assert "packStateLabel" not in findings["opp-service"]

    def test_the_findings_content_is_unchanged(self, client):
        run_id = self._seed_run_with_findings(client)
        before = self._findings(client, run_id)["opp-cloud"]
        _set_state(client, "cloud_ops", STATE_DISABLED)
        after = self._findings(client, run_id)["opp-cloud"]

        # Title, evidence, and the ORIGINAL pack version stamp all survive; the
        # only difference is the additive state label.
        assert after["title"] == before["title"]
        assert after["evidenceIds"] == before["evidenceIds"]
        assert after["packVersion"] == "1.2.0"
        assert after["impact"] == before["impact"]

    def test_the_run_record_and_kv_are_untouched(self, client):
        run_id = self._seed_run_with_findings(client)
        _set_state(client, "cloud_ops", STATE_DISABLED)

        # AC4: disabling deletes nothing — the run and its materialised findings
        # are all still present in the data layer.
        run = db.run_get(run_id)
        assert run is not None
        assert run["packIds"] == ["service_cloud", "cloud_ops"]
        stored: List[Dict[str, Any]] = db.run_kv_get("opps", run_id, [])
        assert len(stored) == 2

    def test_the_label_clears_when_the_pack_is_re_enabled(self, client):
        run_id = self._seed_run_with_findings(client)
        _set_state(client, "cloud_ops", STATE_DISABLED)
        assert (
            self._findings(client, run_id)["opp-cloud"]["packState"]
            == STATE_DISABLED
        )

        _set_state(client, "cloud_ops", STATE_ACTIVE)
        reread = self._findings(client, run_id)["opp-cloud"]
        assert reread["packState"] == STATE_ACTIVE
        assert "packStateLabel" not in reread


# ── AC5 — run health reports state accurately across transitions ──────────────


class TestRunHealthReflectsPackState:
    # Every test here reads the packs panel, which resolves "the latest run for this
    # org" out of the shared contract database — so each needs its own org.
    @pytest.fixture(autouse=True)
    def _own_org(self, isolated_org):
        return isolated_org

    def _seed_executed_run(self, client) -> str:
        run_id = client.post(
            "/api/stack-builder/launch",
            json=_launch_body(pack_ids=["service_cloud", "cloud_ops"]),
            headers=_auth(),
        ).json()["runId"]

        run = db.run_get(run_id)
        # Mimic what the runner persists for an executed multi-pack run.
        run["packs"] = [
            {
                "packId": "service_cloud",
                "packName": "Service Cloud",
                "packVersion": "1.0.0",
                "detectorsExecuted": ["REPETITION"],
                "packExecutedAt": "2026-07-29T10:00:00+00:00",
            },
            {
                "packId": "cloud_ops",
                "packName": "Cloud Operations",
                "packVersion": "1.2.0",
                "detectorsExecuted": ["CLOUD_OPS_QUEUE_AGEING"],
                "packExecutedAt": "2026-07-29T10:00:00+00:00",
            },
        ]
        run["status"] = "complete"
        db.run_set(run_id, run)
        return run_id

    def _packs_panel(self, client) -> Dict[str, Any]:
        response = client.get("/api/run-health/packs", headers=_auth())
        assert response.status_code == 200, response.text
        return response.json()

    def test_all_packs_report_active_before_any_disable(self, client):
        self._seed_executed_run(client)
        rows = {row["pack_id"]: row for row in self._packs_panel(client)["packs"]}
        assert rows["cloud_ops"]["pack_state"] == STATE_ACTIVE
        assert rows["service_cloud"]["pack_state"] == STATE_ACTIVE

    def test_state_flips_to_disabled_while_the_execution_record_is_untouched(
        self, client
    ):
        self._seed_executed_run(client)
        _set_state(client, "cloud_ops", STATE_DISABLED)

        rows = {row["pack_id"]: row for row in self._packs_panel(client)["packs"]}
        assert rows["cloud_ops"]["pack_state"] == STATE_DISABLED
        # The VERSION and detectors that executed are historical facts and must not
        # move when the pack is disabled afterwards.
        assert rows["cloud_ops"]["pack_version"] == "1.2.0"
        assert rows["cloud_ops"]["detectors"] == ["CLOUD_OPS_QUEUE_AGEING"]
        assert rows["service_cloud"]["pack_state"] == STATE_ACTIVE

    def test_state_returns_to_active_on_re_enable(self, client):
        self._seed_executed_run(client)
        _set_state(client, "cloud_ops", STATE_DISABLED)
        _set_state(client, "cloud_ops", STATE_ACTIVE)

        rows = {row["pack_id"]: row for row in self._packs_panel(client)["packs"]}
        assert rows["cloud_ops"]["pack_state"] == STATE_ACTIVE
        assert rows["cloud_ops"]["pack_version"] == "1.2.0"

    def test_panel_reports_the_excluded_pack_for_a_run_launched_while_disabled(
        self, client
    ):
        _set_state(client, "cloud_ops", STATE_DISABLED)
        run_id = client.post(
            "/api/stack-builder/launch",
            json=_launch_body(pack_ids=["service_cloud", "cloud_ops"]),
            headers=_auth(),
        ).json()["runId"]
        run = db.run_get(run_id)
        run["status"] = "complete"
        run["packs"] = [
            {
                "packId": "service_cloud",
                "packName": "Service Cloud",
                "packVersion": "1.0.0",
                "detectorsExecuted": ["REPETITION"],
                "packExecutedAt": "2026-07-29T10:00:00+00:00",
            }
        ]
        db.run_set(run_id, run)

        panel = self._packs_panel(client)
        # An analyst seeing one pack where two were selected gets the reason.
        assert [row["pack_id"] for row in panel["packs"]] == ["service_cloud"]
        assert [row["packId"] for row in panel["excluded_packs"]] == ["cloud_ops"]

    def test_excluded_packs_is_always_present(self, client):
        self._seed_executed_run(client)
        assert "excluded_packs" in self._packs_panel(client)
