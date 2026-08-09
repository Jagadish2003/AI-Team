"""2.0-C1 T4 (AT-829) — run history survives every lifecycle verb, over HTTP.

Parent-story criterion:

  **AC4** — No path in disable / rollback / remove deletes findings, evidence, or run
  records — data-layer test attempting each.

The data-layer proof (that the SQL emitted by each verb contains no DELETE) lives in
``tests/unit/test_never_delete_history_data_layer.py``. This suite closes the loop
end-to-end through the real database: it materialises a run with findings and
evidence, applies **disable**, **rollback**, and **remove** in sequence, and after
each one re-reads everything over the API to prove it is all still there.

It also verifies the guarantee that matters most in practice and is easiest to lose:
a pack REMOVED from the registry must keep its history **reachable**, not merely
present in a table nobody can query.
"""
from __future__ import annotations

import os
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

from app import db
from app.pack_state import (
    InMemoryPackStateStore,
    STATE_ACTIVE,
    STATE_DISABLED,
    set_pack_state_store,
)
from discovery.packs.pack_config import PACK_REGISTRY, get_pack_version

OWNER_TOKEN = os.getenv("DEV_JWT", "dev-token-change-me")
PACK = "cloud_ops"
# PRIOR must stay a literal: it names a real rollback target that has to exist as
# discovery/packs/versions/cloud_ops_pack_config.v1.1.0.json.
PRIOR = "1.1.0"
# CURRENT is READ from the registry, never restated. What these tests assert is
# "the version this run executed is still the version the run record reports" —
# not any particular number. Pinning the literal made a legitimate pack bump
# (2.0-B1 T7 moved cloud_ops 1.2.0 → 1.2.1) fail retention tests that have
# nothing to do with versioning, which trains people to edit the number rather
# than read the failure.
CURRENT = get_pack_version(PACK)


@pytest.fixture(autouse=True)
def _in_memory_pack_state():
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
    with TestClient(app_instance()) as c:
        yield c


def app_instance():
    from app.main import app

    return app


def _auth() -> Dict[str, str]:
    return {"Authorization": f"Bearer {OWNER_TOKEN}"}


@pytest.fixture
def seeded_run(client) -> str:
    """A completed run with two findings and their evidence, materialised."""
    response = client.post(
        "/api/stack-builder/launch",
        json={
            "org_id": "default",
            "selected_system_ids": ["salesforce", "servicenow"],
            "pack_ids": [PACK, "service_cloud"],
            "weightings": {},
        },
        headers=_auth(),
    )
    assert response.status_code == 200, response.text
    run_id = response.json()["runId"]

    db.run_kv_set(
        "opps",
        run_id,
        [
            {
                "id": "opp-cloud",
                "title": "Recurring resolution loop",
                "packId": PACK,
                "packVersion": CURRENT,
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
    db.run_kv_set(
        "evidence",
        run_id,
        [
            {"id": "ev-1", "packId": PACK, "detail": "incident INC001"},
            {"id": "ev-2", "packId": PACK, "detail": "incident INC002"},
            {"id": "ev-3", "packId": "service_cloud", "detail": "case 00123"},
        ],
    )
    run = db.run_get(run_id)
    run["status"] = "complete"
    db.run_set(run_id, run)
    return run_id


def _findings(client, run_id: str) -> Dict[str, Dict[str, Any]]:
    response = client.get(f"/api/runs/{run_id}/opportunities", headers=_auth())
    assert response.status_code == 200, response.text
    return {row["id"]: row for row in response.json()}


def _assert_history_intact(client, run_id: str) -> None:
    """Findings, evidence, and the run record are all still present and stamped."""
    findings = _findings(client, run_id)
    assert set(findings) == {"opp-cloud", "opp-service"}
    assert findings["opp-cloud"]["packVersion"] == CURRENT
    assert findings["opp-cloud"]["evidenceIds"] == ["ev-1", "ev-2"]
    assert findings["opp-service"]["packVersion"] == "1.0.0"

    evidence: List[Dict[str, Any]] = db.run_kv_get("evidence", run_id, [])
    assert [item["id"] for item in evidence] == ["ev-1", "ev-2", "ev-3"]

    run = db.run_get(run_id)
    assert run is not None
    assert run["packIds"] == [PACK, "service_cloud"]
    assert run["packVersions"][PACK] == CURRENT
    assert len(db.run_kv_get("opps", run_id, [])) == 2


# ── Attempting each verb, end to end ──────────────────────────────────────────


class TestDisableRetainsHistory:
    def test_history_is_intact_before_any_transition(self, client, seeded_run):
        _assert_history_intact(client, seeded_run)

    def test_disabling_deletes_nothing(self, client, seeded_run):
        response = client.put(
            f"/api/packs/{PACK}/state",
            json={"state": STATE_DISABLED, "reason": "opted out"},
            headers=_auth(),
        )
        assert response.status_code == 200, response.text
        _assert_history_intact(client, seeded_run)

    def test_the_disabled_packs_finding_is_labelled_not_removed(
        self, client, seeded_run
    ):
        client.put(
            f"/api/packs/{PACK}/state",
            json={"state": STATE_DISABLED},
            headers=_auth(),
        )
        findings = _findings(client, seeded_run)
        assert findings["opp-cloud"]["packState"] == STATE_DISABLED
        assert findings["opp-cloud"]["packStateLabel"]
        # …and the other pack's finding is untouched.
        assert findings["opp-service"]["packState"] == STATE_ACTIVE

    def test_re_enabling_deletes_nothing(self, client, seeded_run):
        client.put(
            f"/api/packs/{PACK}/state",
            json={"state": STATE_DISABLED},
            headers=_auth(),
        )
        client.put(
            f"/api/packs/{PACK}/state",
            json={"state": STATE_ACTIVE},
            headers=_auth(),
        )
        _assert_history_intact(client, seeded_run)


class TestRollbackRetainsHistory:
    def test_rolling_back_deletes_nothing(self, client, seeded_run):
        response = client.put(
            f"/api/packs/{PACK}/version",
            json={"version": PRIOR, "reason": "regression"},
            headers=_auth(),
        )
        assert response.status_code == 200, response.text
        _assert_history_intact(client, seeded_run)

    def test_the_earlier_runs_version_stamp_is_not_rewritten(self, client, seeded_run):
        client.put(
            f"/api/packs/{PACK}/version", json={"version": PRIOR}, headers=_auth()
        )
        # The run executed 1.2.0 and still says so, findings included.
        assert db.run_get(seeded_run)["packVersions"][PACK] == CURRENT
        assert _findings(client, seeded_run)["opp-cloud"]["packVersion"] == CURRENT

    def test_restoring_deletes_nothing(self, client, seeded_run):
        client.put(
            f"/api/packs/{PACK}/version", json={"version": PRIOR}, headers=_auth()
        )
        client.put(
            f"/api/packs/{PACK}/version", json={"version": None}, headers=_auth()
        )
        _assert_history_intact(client, seeded_run)


class TestRemoveRetainsHistory:
    """A pack REMOVED from the registry keeps its history present AND reachable."""

    @pytest.fixture
    def removed_pack(self, monkeypatch):
        original = PACK_REGISTRY[PACK]
        monkeypatch.delitem(PACK_REGISTRY, PACK)
        return original

    def test_removal_deletes_nothing(self, client, seeded_run, removed_pack):
        assert PACK not in PACK_REGISTRY
        _assert_history_intact(client, seeded_run)

    def test_a_removed_packs_findings_are_still_served_over_the_api(
        self, client, seeded_run, removed_pack
    ):
        findings = _findings(client, seeded_run)
        assert "opp-cloud" in findings
        assert findings["opp-cloud"]["packId"] == PACK
        assert findings["opp-cloud"]["packVersion"] == CURRENT

    def test_a_removed_packs_run_is_still_readable_over_the_api(
        self, client, seeded_run, removed_pack
    ):
        response = client.get(f"/api/runs/{seeded_run}", headers=_auth())
        assert response.status_code == 200, response.text
        assert response.json()["packVersions"][PACK] == CURRENT

    def test_run_health_still_renders_for_a_removed_pack(
        self, client, seeded_run, removed_pack
    ):
        response = client.get("/api/run-health/packs", headers=_auth())
        assert response.status_code == 200, response.text

    def test_a_removed_packs_lifecycle_history_is_still_reachable(
        self, client, seeded_run, monkeypatch
    ):
        # Disable and roll back FIRST, then remove the pack — the realistic order.
        client.put(
            f"/api/packs/{PACK}/version",
            json={"version": PRIOR, "reason": "regression"},
            headers=_auth(),
        )
        client.put(
            f"/api/packs/{PACK}/state",
            json={"state": STATE_DISABLED, "reason": "retiring it"},
            headers=_auth(),
        )
        monkeypatch.delitem(PACK_REGISTRY, PACK)

        # The trail must NOT 404 just because the registry moved on — history you
        # cannot reach is functionally deleted.
        response = client.get(f"/api/packs/{PACK}/state/history", headers=_auth())
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["registered"] is False
        assert [t["transition"] for t in body["transitions"]] == [
            "disable",
            "rollback",
        ]

    def test_a_removed_packs_state_row_is_still_listed(
        self, client, seeded_run, monkeypatch
    ):
        client.put(
            f"/api/packs/{PACK}/state",
            json={"state": STATE_DISABLED, "reason": "retiring it"},
            headers=_auth(),
        )
        monkeypatch.delitem(PACK_REGISTRY, PACK)

        rows = {
            row["packId"]: row
            for row in client.get("/api/packs/state", headers=_auth()).json()["packs"]
        }
        assert PACK in rows
        assert rows[PACK]["registered"] is False
        assert rows[PACK]["state"] == STATE_DISABLED
        assert rows[PACK]["reason"] == "retiring it"

    def test_a_genuinely_unknown_pack_is_still_404(self, client):
        # The removed-pack allowance must not turn a typo into a 200.
        response = client.get(
            "/api/packs/no_such_pack_at_all/state/history", headers=_auth()
        )
        assert response.status_code == 404

    def test_a_removed_pack_cannot_be_transitioned(self, client, removed_pack):
        # It is gone: there is nothing to disable. Reads stay open, writes 404.
        response = client.put(
            f"/api/packs/{PACK}/state",
            json={"state": STATE_DISABLED},
            headers=_auth(),
        )
        assert response.status_code == 404


class TestAllThreeVerbsInSequence:
    def test_history_survives_disable_then_rollback_then_remove(
        self, client, seeded_run, monkeypatch
    ):
        # The full lifecycle, in the order a real customer would apply it.
        client.put(
            f"/api/packs/{PACK}/state",
            json={"state": STATE_DISABLED},
            headers=_auth(),
        )
        _assert_history_intact(client, seeded_run)

        client.put(
            f"/api/packs/{PACK}/state", json={"state": STATE_ACTIVE}, headers=_auth()
        )
        client.put(
            f"/api/packs/{PACK}/version", json={"version": PRIOR}, headers=_auth()
        )
        _assert_history_intact(client, seeded_run)

        monkeypatch.delitem(PACK_REGISTRY, PACK)
        _assert_history_intact(client, seeded_run)

    def test_the_run_event_log_survives_every_verb(self, client, seeded_run):
        db.run_kv_set(
            "events", seeded_run, [{"stage": "INGEST", "message": "seeded"}]
        )
        before = db.run_kv_get("events", seeded_run, [])
        assert before

        client.put(
            f"/api/packs/{PACK}/state",
            json={"state": STATE_DISABLED},
            headers=_auth(),
        )
        client.put(
            f"/api/packs/{PACK}/version", json={"version": PRIOR}, headers=_auth()
        )
        assert db.run_kv_get("events", seeded_run, []) == before
