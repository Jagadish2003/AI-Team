"""2.0-C1 T3 (AT-828) — pack version rollback over the HTTP surface.

Parent-story criteria exercised here:

  * **AC3** — rollback causes subsequent runs to use the prior version; existing
    findings retain their original version stamps; nothing is rewritten retroactively.
  * **AC4** (contributing) — rollback deletes nothing: the run record, its
    opportunities, and their version stamps all survive.
  * **AC5** — run health reports pack version accurately across the
    current → rolled-back → restored transitions.

The state machine and resolution are pinned DB-free in
``tests/unit/test_pack_version_rollback.py``; this suite pins the API contract, the
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
    InMemoryPackStateStore,
    STATE_DISABLED,
    set_pack_state_store,
)
from app.rbac import seed_owner

OWNER_TOKEN = os.getenv("DEV_JWT", "dev-token-change-me")
ANALYST_TOKEN = "analyst-token"
VIEWER_TOKEN = "viewer-token"

PRIOR = "1.1.0"
CURRENT = "1.2.0"

# The org requests are scoped to, or None to send NO X-Org-Id header — the default, so
# every currently-passing test keeps its exact previous request shape. Only the
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

    Required by anything asserting on `GET /api/run-health/packs`:
    `health_aggregation._latest_run` returns the newest run **for the org across the
    whole contract database**, which is dropped only once per session. Other suites
    create runs in the shared `default` org, so "the run I just launched is the
    latest" holds in isolation and breaks in a full run.
    """
    previous = _CURRENT_ORG["id"]
    _CURRENT_ORG["id"] = _owner_org("pack_rollback")
    try:
        yield _CURRENT_ORG["id"]
    finally:
        _CURRENT_ORG["id"] = previous


@pytest.fixture(autouse=True)
def _role_tokens(monkeypatch):
    monkeypatch.setenv("ANALYST_JWT", ANALYST_TOKEN)
    monkeypatch.setenv("VIEWER_JWT", VIEWER_TOKEN)
    yield


@pytest.fixture(autouse=True)
def _in_memory_pack_state():
    """Isolate pack state per test so no test leaves a pack pinned for another."""
    set_pack_state_store(InMemoryPackStateStore())
    yield
    set_pack_state_store(None)


@pytest.fixture(autouse=True)
def _clean_version_context():
    from discovery.packs.pack_version_context import set_pack_config_paths

    set_pack_config_paths({})
    yield
    set_pack_config_paths({})


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


def _set_version(client, pack_id: str, version, **kwargs) -> Any:
    payload: Dict[str, Any] = {"version": version}
    payload.update(kwargs)
    return client.put(
        f"/api/packs/{pack_id}/version", json=payload, headers=_auth()
    )


def _launch_body(**overrides: Any) -> Dict[str, Any]:
    """A launch payload, defaulting to a single `cloud_ops` selection.

    When the caller supplies ``pack_ids`` the default singular ``pack_id`` is DROPPED:
    ``LaunchRequest`` reconciles the two into one selection, so leaving it in would
    silently add `cloud_ops` to any explicit selection.
    """
    body: Dict[str, Any] = {
        "org_id": "default",
        "selected_system_ids": ["salesforce", "servicenow"],
        "weightings": {},
    }
    if "pack_ids" not in overrides:
        body["pack_id"] = "cloud_ops"
    body.update(overrides)
    return body


# ── The version endpoint ──────────────────────────────────────────────────────


class TestPackVersionEndpoint:
    def test_owner_can_roll_a_pack_back(self, client):
        response = _set_version(
            client, "cloud_ops", PRIOR, reason="1.2.0 threshold regression"
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["transition"] == "rollback"
        assert body["changed"] is True
        assert body["pinnedVersion"] == PRIOR
        assert body["previousPinnedVersion"] is None
        assert body["currentVersion"] == CURRENT
        assert body["effectiveVersion"] == PRIOR
        assert body["reason"] == "1.2.0 threshold regression"

    def test_rollback_is_reflected_in_the_state_list(self, client):
        _set_version(client, "cloud_ops", PRIOR)
        rows = {
            row["packId"]: row
            for row in client.get("/api/packs/state", headers=_auth()).json()["packs"]
        }
        assert rows["cloud_ops"]["pinnedVersion"] == PRIOR
        assert rows["cloud_ops"]["effectiveVersion"] == PRIOR
        # The registry still SHIPS the current version — the pin is per-org config.
        assert rows["cloud_ops"]["packVersion"] == CURRENT
        assert rows["cloud_ops"]["availableVersions"] == [PRIOR]

    def test_repeating_a_rollback_is_idempotent(self, client):
        _set_version(client, "cloud_ops", PRIOR)
        again = _set_version(client, "cloud_ops", PRIOR)
        assert again.status_code == 200
        assert again.json()["changed"] is False

    def test_null_version_restores_the_current_version(self, client):
        _set_version(client, "cloud_ops", PRIOR)
        response = _set_version(client, "cloud_ops", None)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["transition"] == "restore"
        assert body["pinnedVersion"] is None
        assert body["previousPinnedVersion"] == PRIOR
        assert body["effectiveVersion"] == CURRENT

    def test_unavailable_version_is_409_naming_what_is_available(self, client):
        response = _set_version(client, "cloud_ops", "9.9.9")
        assert response.status_code == 409, response.text
        detail = response.json()["detail"]
        assert "9.9.9" in detail
        assert PRIOR in detail

    def test_code_only_pack_rollback_is_409(self, client):
        response = _set_version(client, "service_cloud", "0.9.0")
        assert response.status_code == 409, response.text
        assert "no archived prior versions" in response.json()["detail"]

    def test_unknown_pack_is_404(self, client):
        assert _set_version(client, "no_such_pack", PRIOR).status_code == 404

    def test_history_records_the_rollback_with_both_versions(self, client):
        _set_version(client, "cloud_ops", PRIOR, reason="regression")
        _set_version(client, "cloud_ops", None, reason="fixed forward")
        transitions = client.get(
            "/api/packs/cloud_ops/state/history", headers=_auth()
        ).json()["transitions"]
        assert [t["transition"] for t in transitions] == ["restore", "rollback"]
        assert transitions[1]["resulting_version"] == PRIOR
        assert transitions[1]["previous_version"] is None
        assert transitions[0]["previous_version"] == PRIOR
        assert transitions[0]["resulting_version"] is None


class TestPackVersionRbac:
    def test_analyst_cannot_roll_back(self, client):
        response = client.put(
            "/api/packs/cloud_ops/version",
            json={"version": PRIOR},
            headers=_auth(ANALYST_TOKEN),
        )
        assert response.status_code == 403, response.text

    def test_viewer_cannot_roll_back(self, client):
        response = client.put(
            "/api/packs/cloud_ops/version",
            json={"version": PRIOR},
            headers=_auth(VIEWER_TOKEN),
        )
        assert response.status_code == 403, response.text

    def test_viewer_can_read_the_effective_version(self, client):
        _set_version(client, "cloud_ops", PRIOR)
        response = client.get("/api/packs/state", headers=_auth(VIEWER_TOKEN))
        assert response.status_code == 200, response.text
        rows = {row["packId"]: row for row in response.json()["packs"]}
        assert rows["cloud_ops"]["effectiveVersion"] == PRIOR


# ── AC3 — runs after rollback use the prior version ───────────────────────────


class TestRunsAfterRollbackUseThePriorVersion:
    def test_launch_records_the_pinned_version_as_the_pack_version(self, client):
        _set_version(client, "cloud_ops", PRIOR)
        response = client.post(
            "/api/stack-builder/launch", json=_launch_body(), headers=_auth()
        )
        assert response.status_code == 200, response.text
        run_id = response.json()["runId"]

        run = db.run_get(run_id)
        # The run will EXECUTE 1.1.0, so that is what its version record says —
        # recording 1.2.0 would make the run disagree with its own findings.
        assert run["packVersions"]["cloud_ops"] == PRIOR
        assert run["pinnedPackVersions"] == {"cloud_ops": PRIOR}
        assert db.run_kv_get("pinned_pack_versions", run_id, {}) == {
            "cloud_ops": PRIOR
        }
        assert db.run_kv_get("pack_versions", run_id, {})["cloud_ops"] == PRIOR

    def test_launch_without_a_rollback_records_the_current_version(self, client):
        response = client.post(
            "/api/stack-builder/launch", json=_launch_body(), headers=_auth()
        )
        run = db.run_get(response.json()["runId"])
        assert run["packVersions"]["cloud_ops"] == CURRENT
        assert run["pinnedPackVersions"] == {}

    def test_only_the_rolled_back_pack_is_affected(self, client):
        _set_version(client, "cloud_ops", PRIOR)
        response = client.post(
            "/api/stack-builder/launch",
            json=_launch_body(pack_ids=["cloud_ops", "security_ops"]),
            headers=_auth(),
        )
        run = db.run_get(response.json()["runId"])
        assert run["packVersions"]["cloud_ops"] == PRIOR
        assert run["packVersions"]["security_ops"] == CURRENT
        assert run["pinnedPackVersions"] == {"cloud_ops": PRIOR}

    def test_a_restored_pack_returns_to_the_current_version(self, client):
        _set_version(client, "cloud_ops", PRIOR)
        _set_version(client, "cloud_ops", None)
        response = client.post(
            "/api/stack-builder/launch", json=_launch_body(), headers=_auth()
        )
        run = db.run_get(response.json()["runId"])
        assert run["packVersions"]["cloud_ops"] == CURRENT
        assert run["pinnedPackVersions"] == {}

    def test_a_rolled_back_and_disabled_pack_does_not_run_at_all(self, client):
        _set_version(client, "cloud_ops", PRIOR)
        client.put(
            "/api/packs/cloud_ops/state",
            json={"state": STATE_DISABLED},
            headers=_auth(),
        )
        response = client.post(
            "/api/stack-builder/launch",
            json=_launch_body(pack_ids=["cloud_ops", "security_ops"]),
            headers=_auth(),
        )
        assert response.status_code == 200, response.text
        run = db.run_get(response.json()["runId"])
        assert run["packIds"] == ["security_ops"]
        # Not running ⇒ no version for it to run at.
        assert run["pinnedPackVersions"] == {}
        assert "cloud_ops" not in run["packVersions"]


# ── AC3 / AC4 — nothing is rewritten retroactively ────────────────────────────


class TestHistoricalRunsAreUntouched:
    def _seed_run_with_findings(self, client) -> str:
        run_id = client.post(
            "/api/stack-builder/launch", json=_launch_body(), headers=_auth()
        ).json()["runId"]
        db.run_kv_set(
            "opps",
            run_id,
            [
                {
                    "id": "opp-1",
                    "title": "Recurring resolution loop",
                    "packId": "cloud_ops",
                    "packVersion": CURRENT,
                    "impact": 8.0,
                    "effort": 3.0,
                    "evidenceIds": ["ev-1", "ev-2"],
                    "decision": "UNREVIEWED",
                }
            ],
        )
        return run_id

    def _findings(self, client, run_id: str) -> List[Dict[str, Any]]:
        response = client.get(
            f"/api/runs/{run_id}/opportunities", headers=_auth()
        )
        assert response.status_code == 200, response.text
        return response.json()

    def test_existing_findings_keep_their_original_version_stamp(self, client):
        run_id = self._seed_run_with_findings(client)
        _set_version(client, "cloud_ops", PRIOR)

        findings = self._findings(client, run_id)
        assert len(findings) == 1
        # THE criterion: a finding produced by 1.2.0 still says 1.2.0 after the
        # rollback. Rewriting it would destroy the provenance R16-B1 §4 exists for.
        assert findings[0]["packVersion"] == CURRENT
        assert findings[0]["packId"] == "cloud_ops"

    def test_findings_remain_fully_retrievable(self, client):
        run_id = self._seed_run_with_findings(client)
        before = self._findings(client, run_id)[0]
        _set_version(client, "cloud_ops", PRIOR)
        after = self._findings(client, run_id)[0]
        assert after["title"] == before["title"]
        assert after["evidenceIds"] == before["evidenceIds"]
        assert after["impact"] == before["impact"]

    def test_the_earlier_runs_record_is_not_rewritten(self, client):
        run_id = self._seed_run_with_findings(client)
        before = db.run_get(run_id)
        assert before["packVersions"]["cloud_ops"] == CURRENT

        _set_version(client, "cloud_ops", PRIOR)

        after = db.run_get(run_id)
        # No backfill: the run still records the version it actually ran.
        assert after["packVersions"]["cloud_ops"] == CURRENT
        assert after["pinnedPackVersions"] == {}
        assert len(db.run_kv_get("opps", run_id, [])) == 1

    def test_a_later_run_uses_the_pin_while_the_earlier_one_does_not(self, client):
        earlier = self._seed_run_with_findings(client)
        _set_version(client, "cloud_ops", PRIOR)
        later = db.run_get(
            client.post(
                "/api/stack-builder/launch", json=_launch_body(), headers=_auth()
            ).json()["runId"]
        )
        assert db.run_get(earlier)["packVersions"]["cloud_ops"] == CURRENT
        assert later["packVersions"]["cloud_ops"] == PRIOR

    def test_restoring_does_not_rewrite_the_rolled_back_run(self, client):
        _set_version(client, "cloud_ops", PRIOR)
        pinned_run = client.post(
            "/api/stack-builder/launch", json=_launch_body(), headers=_auth()
        ).json()["runId"]
        assert db.run_get(pinned_run)["packVersions"]["cloud_ops"] == PRIOR

        _set_version(client, "cloud_ops", None)

        # The run that DID use 1.1.0 still says so after the restore.
        assert db.run_get(pinned_run)["packVersions"]["cloud_ops"] == PRIOR
        assert db.run_get(pinned_run)["pinnedPackVersions"] == {"cloud_ops": PRIOR}


# ── AC5 — run health reports versions accurately across transitions ───────────


class TestRunHealthReflectsVersions:
    # Every test here reads the packs panel, which resolves "the latest run for this
    # org" out of the shared contract database — so each needs its own org.
    @pytest.fixture(autouse=True)
    def _own_org(self, isolated_org):
        return isolated_org

    def _seed_executed_run(self, client, *, pack_version: str) -> str:
        run_id = client.post(
            "/api/stack-builder/launch", json=_launch_body(), headers=_auth()
        ).json()["runId"]
        run = db.run_get(run_id)
        run["status"] = "complete"
        run["packs"] = [
            {
                "packId": "cloud_ops",
                "packName": "Cloud Operations",
                "packVersion": pack_version,
                "detectorsExecuted": ["CLOUD_OPS_QUEUE_AGEING"],
                "packExecutedAt": "2026-07-29T10:00:00+00:00",
            }
        ]
        db.run_set(run_id, run)
        return run_id

    def _packs_panel(self, client) -> Dict[str, Any]:
        response = client.get("/api/run-health/packs", headers=_auth())
        assert response.status_code == 200, response.text
        return response.json()

    def test_an_unpinned_run_reports_no_rollback(self, client):
        self._seed_executed_run(client, pack_version=CURRENT)
        panel = self._packs_panel(client)
        row = {r["pack_id"]: r for r in panel["packs"]}["cloud_ops"]
        assert row["pack_version"] == CURRENT
        assert row["pinned_version"] is None
        assert row["rolled_back"] is False
        assert panel["pinned_pack_versions"] == {}

    def test_a_pinned_run_reports_the_rollback(self, client):
        _set_version(client, "cloud_ops", PRIOR)
        self._seed_executed_run(client, pack_version=PRIOR)
        panel = self._packs_panel(client)
        row = {r["pack_id"]: r for r in panel["packs"]}["cloud_ops"]
        assert row["pack_version"] == PRIOR
        assert row["pinned_version"] == PRIOR
        assert row["rolled_back"] is True
        assert panel["pinned_pack_versions"] == {"cloud_ops": PRIOR}

    def test_restoring_after_a_run_does_not_change_what_that_run_reports(
        self, client
    ):
        _set_version(client, "cloud_ops", PRIOR)
        self._seed_executed_run(client, pack_version=PRIOR)
        _set_version(client, "cloud_ops", None)

        # The panel reads the run's HISTORICAL pin, not the org's current one, so a
        # later restore cannot rewrite what this run says it did (AC3).
        panel = self._packs_panel(client)
        row = {r["pack_id"]: r for r in panel["packs"]}["cloud_ops"]
        assert row["pack_version"] == PRIOR
        assert row["pinned_version"] == PRIOR
        assert row["rolled_back"] is True

    def test_rolling_back_after_a_run_does_not_backdate_that_run(self, client):
        self._seed_executed_run(client, pack_version=CURRENT)
        _set_version(client, "cloud_ops", PRIOR)

        panel = self._packs_panel(client)
        row = {r["pack_id"]: r for r in panel["packs"]}["cloud_ops"]
        # The run executed 1.2.0 and still says so, even though the org is now
        # pinned to 1.1.0 for future runs.
        assert row["pack_version"] == CURRENT
        assert row["pinned_version"] is None
        assert row["rolled_back"] is False

    def test_pinned_pack_versions_is_always_present(self, client):
        self._seed_executed_run(client, pack_version=CURRENT)
        assert "pinned_pack_versions" in self._packs_panel(client)
