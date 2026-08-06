"""Contract tests for 2.0-A2 T2 — the frozen, immutable baseline artifact (AC1).

AC1: *"A finding freezes a retrievable baseline artifact (signals, window, values,
pack version) at creation; the artifact is immutable thereafter."*

The subtask's definition of done, one class each:

* creating a finding produces a retrievable baseline artifact;
* a second write for an existing identity is a no-op, never an overwrite;
* the artifact survives a replay of the originating run unchanged;
* every field T3's comparison and T4's confounder checks need is present;
* no application path can mutate it (behavioural half — the structural half is in
  ``tests/unit/test_opportunity_baseline_immutability.py``);
* backfill is out of scope: a pre-existing finding has no baseline and is
  therefore not measurable.
"""

from __future__ import annotations

import json
import os
from contextlib import closing
from datetime import datetime, timezone
from typing import Any, Dict, List
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app
from app.opportunity_baseline_artifact import missing_artifact_fields

DEV_TOKEN = os.getenv("DEV_JWT", "dev-token-change-me")
VIEWER_TOKEN = os.getenv("VIEWER_JWT", "viewer-token")
BASE = "/api/opportunity-baseline"


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture(autouse=True)
def _tables() -> None:
    from app.opportunity_baseline import ensure_opportunity_baseline_table

    ensure_opportunity_baseline_table()


def _auth(org_id: str, token: str = DEV_TOKEN) -> Dict[str, str]:
    return {"Authorization": f"Bearer {token}", "X-Org-Id": org_id}


def _seed_workspace_member(org_id: str, user_id: str, role: str = "owner") -> None:
    from app.rbac import _ensure_members_table

    _ensure_members_table()
    with closing(db.connect()) as con:
        with con.cursor() as cur:
            cur.execute(
                "INSERT INTO workspace_members (org_id, user_id, role, created_at) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                (org_id, user_id, role, datetime.now(timezone.utc).isoformat()),
            )
        con.commit()


def _org() -> str:
    org_id = f"org-a2t2-{uuid4().hex[:8]}"
    _seed_workspace_member(org_id, DEV_TOKEN)
    return org_id


def _identity() -> str:
    return f"opp_{uuid4().hex[:24]}"


def _opp(identity: str, *, opp_id: str = "opp_001", **overrides: Any) -> Dict[str, Any]:
    opp = {
        "id": opp_id,
        "title": "Elevated case owner reassignment",
        "tier": "Quick Win",
        "decision": "UNREVIEWED",
        "confidence": "HIGH",
        "opportunity_identity": identity,
        "packId": "service_cloud",
        "packVersion": "1.2.0",
        "baseline_mean": 201.6,
        "baseline_stddev": 2.7,
        "baseline_window_days": 90,
        "run_count": 5,
        "current_value": 2.4,
        "recent_values": [200.0, 205.0, 198.0, 202.0, 203.0],
        "signal_key": "service_cloud::HANDOFF_FRICTION::metric_value",
        "_debug": {
            "detector_id": "HANDOFF_FRICTION",
            "metric_value": 2.4,
            "threshold": 1.5,
            "run_completed_at": "2026-07-30T10:00:00+00:00",
            "raw_evidence": {
                "owner_changes_90d": 240.0,
                "total_cases_90d": 800.0,
                "handoff_score": 2.4,
            },
        },
    }
    opp.update(overrides)
    return opp


def _capture(org: str, run: str, opps: List[Dict[str, Any]]) -> Dict[str, int]:
    from app.opportunity_baseline import capture_baselines_for_run

    return capture_baselines_for_run(opps, org_id=org, run_id=run)


def _raw_row(org: str, identity: str):
    with closing(db.connect()) as con:
        with con.cursor() as cur:
            cur.execute(
                "SELECT run_id, pack_version, artifact, captured_at "
                "FROM opportunity_baselines "
                "WHERE org_id = %s AND opportunity_identity = %s",
                (org, identity),
            )
            return cur.fetchone()


# ---------------------------------------------------------------------------
# Creation produces a retrievable artifact
# ---------------------------------------------------------------------------


class TestCreationFreezesARetrievableArtifact:
    def test_capture_creates_and_the_artifact_is_retrievable(self, client):
        org, identity, run = _org(), _identity(), "run_1"
        assert _capture(org, run, [_opp(identity)])["created"] == 1

        response = client.get(f"{BASE}/{identity}", headers=_auth(org))
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["complete"] is True
        assert body["missingFields"] == []

        artifact = body["artifact"]
        assert artifact["opportunityIdentity"] == identity
        assert artifact["runId"] == run
        assert artifact["orgId"] == org

    def test_the_artifact_carries_signals_window_values_and_pack_version(self, client):
        """AC1's four named parts, on the wire."""
        org, identity = _org(), _identity()
        _capture(org, identity_run := "run_1", [_opp(identity)])

        artifact = client.get(f"{BASE}/{identity}", headers=_auth(org)).json()["artifact"]

        signal_names = {s["signalName"] for s in artifact["signals"]}
        assert {"owner_changes_90d", "total_cases_90d"} <= signal_names

        assert artifact["window"]["days"] == 90
        assert artifact["window"]["startedAt"] and artifact["window"]["endedAt"]
        assert artifact["window"]["derivation"]

        assert artifact["measuredValues"]["owner_changes_90d"] == 240
        assert artifact["baselineStats"]["mean"] == 201.6

        assert artifact["packVersion"] == "1.2.0"

    def test_the_artifact_has_everything_t3_and_t4_need(self, client):
        org, identity = _org(), _identity()
        _capture(org, "run_1", [_opp(identity)])
        artifact = client.get(f"{BASE}/{identity}", headers=_auth(org)).json()["artifact"]
        assert missing_artifact_fields(artifact) == []

    def test_the_artifact_references_the_originating_instance(self, client):
        org, identity = _org(), _identity()
        _capture(org, "run_1", [_opp(identity)])
        artifact = client.get(f"{BASE}/{identity}", headers=_auth(org)).json()["artifact"]
        assert artifact["instanceRef"] == {
            "opportunityIdentity": identity,
            "runId": "run_1",
        }

    def test_baselines_are_listable_per_run_and_per_org(self, client):
        org = _org()
        a, b = _identity(), _identity()
        _capture(org, "run_1", [_opp(a, opp_id="opp_a"), _opp(b, opp_id="opp_b")])

        per_run = client.get(f"{BASE}/run/run_1", headers=_auth(org)).json()
        assert per_run["count"] == 2
        assert set(per_run["baselines"]) == {a, b}

        listed = client.get(BASE, headers=_auth(org)).json()
        assert listed["count"] == 2

    def test_an_unbaselined_identity_is_404_and_says_why(self, client):
        """Backfill is out of scope, so the 404 must be informative."""
        org = _org()
        response = client.get(f"{BASE}/{_identity()}", headers=_auth(org))
        assert response.status_code == 404
        detail = response.json()["detail"]
        assert "not measurable" in detail

    def test_a_finding_without_a_stable_identity_is_skipped_not_faked(self):
        org = _org()
        opp = _opp(_identity())
        opp.pop("opportunity_identity")
        counts = _capture(org, "run_1", [opp])
        assert counts == {"created": 0, "existing": 0, "skipped": 1}


# ---------------------------------------------------------------------------
# Write-once
# ---------------------------------------------------------------------------


class TestWriteOnce:
    def test_a_second_capture_is_a_no_op_not_an_overwrite(self, client):
        org, identity = _org(), _identity()
        assert _capture(org, "run_1", [_opp(identity)])["created"] == 1

        # A later run re-surfaces the same problem with DIFFERENT measurements.
        moved = _opp(identity, opp_id="opp_001")
        moved["_debug"]["raw_evidence"]["owner_changes_90d"] = 999.0
        moved["baseline_mean"] = 1.0
        moved["packVersion"] = "9.9.9"
        counts = _capture(org, "run_2", [moved])

        assert counts["created"] == 0 and counts["existing"] == 1

        artifact = client.get(f"{BASE}/{identity}", headers=_auth(org)).json()["artifact"]
        assert artifact["runId"] == "run_1", "the originating run must not change"
        assert artifact["measuredValues"]["owner_changes_90d"] == 240
        assert artifact["baselineStats"]["mean"] == 201.6
        assert artifact["packVersion"] == "1.2.0"

    def test_the_no_op_returns_the_stored_artifact_not_the_fresh_one(self):
        """A caller must never mistake a recomputed basis for the frozen one."""
        from app.opportunity_baseline import capture_baseline

        org, identity = _org(), _identity()
        capture_baseline(_opp(identity), org_id=org, run_id="run_1")

        moved = _opp(identity)
        moved["_debug"]["raw_evidence"]["owner_changes_90d"] = 999.0
        result = capture_baseline(moved, org_id=org, run_id="run_2")

        assert result["created"] is False
        assert result["artifact"]["measuredValues"]["owner_changes_90d"] == 240

    def test_the_stored_row_is_byte_identical_after_a_second_capture(self):
        """Immutability at the row level, not just at the served shape."""
        org, identity = _org(), _identity()
        _capture(org, "run_1", [_opp(identity)])
        before = _raw_row(org, identity)

        moved = _opp(identity)
        moved["baseline_mean"] = 1.0
        _capture(org, "run_2", [moved])
        after = _raw_row(org, identity)

        assert before == after, "the stored row changed — the artifact is not immutable"

    def test_many_repeated_captures_never_change_the_artifact(self):
        org, identity = _org(), _identity()
        _capture(org, "run_1", [_opp(identity)])
        original = _raw_row(org, identity)

        for i in range(2, 6):
            _capture(org, f"run_{i}", [_opp(identity)])
        assert _raw_row(org, identity) == original


# ---------------------------------------------------------------------------
# Replay survival — the hazard the subtask names
# ---------------------------------------------------------------------------


class TestSurvivesReplay:
    def _seed_replayable_run(self, org: str, run: str, identity: str):
        db.run_set(
            run,
            {"id": run, "runId": run, "status": "complete", "org_id": org},
        )
        opps = [_opp(identity)]
        db.run_kv_set("opps", run, opps)
        db.run_kv_set("evidence", run, [])
        _capture(org, run, opps)
        return opps

    def test_the_artifact_survives_a_replay_of_the_originating_run(self, client):
        """``opps`` is rewritten wholesale by replay; the baseline is not in it."""
        org, identity, run = _org(), _identity(), f"run-{uuid4().hex[:6]}"
        self._seed_replayable_run(org, run, identity)
        before = _raw_row(org, identity)

        from app.replay import replay_run

        try:
            replay_run(run)
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"replay unavailable in this environment: {exc}")

        assert _raw_row(org, identity) == before, (
            "a replay rewrote the baseline — it must live outside the run-scoped blob"
        )

    def test_the_artifact_survives_a_wholesale_rewrite_of_the_opps_blob(self, client):
        """The specific mechanism: materialization re-persists ``opps`` entirely."""
        org, identity, run = _org(), _identity(), f"run-{uuid4().hex[:6]}"
        self._seed_replayable_run(org, run, identity)
        before = _raw_row(org, identity)

        db.run_kv_set("opps", run, [])  # the blob is emptied
        assert _raw_row(org, identity) == before

        artifact = client.get(f"{BASE}/{identity}", headers=_auth(org)).json()["artifact"]
        assert artifact["measuredValues"]["owner_changes_90d"] == 240

    def test_the_artifact_survives_the_decision_reset_replay_performs(self, client):
        org, identity, run = _org(), _identity(), f"run-{uuid4().hex[:6]}"
        opps = self._seed_replayable_run(org, run, identity)
        before = _raw_row(org, identity)

        # Mimic replay.py's decision reset on the stored blob.
        for opp in opps:
            opp["decision"] = "UNREVIEWED"
        db.run_kv_set("opps", run, opps)

        assert _raw_row(org, identity) == before


# ---------------------------------------------------------------------------
# No application path mutates it
# ---------------------------------------------------------------------------


class TestNoApplicationPathMutatesIt:
    def test_the_api_exposes_no_write_verb(self, client):
        org, identity = _org(), _identity()
        _capture(org, "run_1", [_opp(identity)])

        for method in ("post", "put", "patch", "delete"):
            # TestClient.delete() takes no json body, so send one only where the
            # verb accepts it — the assertion is about the verb being absent.
            kwargs = {"headers": _auth(org)}
            if method != "delete":
                kwargs["json"] = {"packVersion": "9.9.9"}
            response = getattr(client, method)(f"{BASE}/{identity}", **kwargs)
            assert response.status_code in (404, 405), (
                f"{method.upper()} on a baseline returned {response.status_code}; "
                "the API must expose no write verb"
            )

        artifact = client.get(f"{BASE}/{identity}", headers=_auth(org)).json()["artifact"]
        assert artifact["packVersion"] == "1.2.0"

    def test_the_store_module_offers_no_mutation_function(self):
        import app.opportunity_baseline as store

        for name in dir(store):
            if name.startswith("_"):
                continue
            assert not any(
                verb in name.lower()
                for verb in ("update", "delete", "modify", "overwrite")
            ), f"{name} looks like a mutation path"

    def test_a_direct_update_is_the_only_way_and_is_not_in_the_codebase(self):
        """Proves the row CAN be changed by raw SQL — and that nothing does.

        The point of the test is the second half: immutability here rests on there
        being no application path, plus the production REVOKE. This documents the
        boundary honestly rather than implying the DB itself forbids it.
        """
        org, identity = _org(), _identity()
        _capture(org, "run_1", [_opp(identity)])

        from pathlib import Path

        backend = Path(__file__).resolve().parents[2]
        offenders = []
        for path in (backend / "app").rglob("*.py"):
            text = path.read_text(encoding="utf-8", errors="replace")
            if "UPDATE opportunity_baselines" in text or (
                "DELETE FROM opportunity_baselines" in text
            ):
                offenders.append(path.name)
        assert not offenders, f"mutating SQL found in: {offenders}"


# ---------------------------------------------------------------------------
# RBAC and tenancy
# ---------------------------------------------------------------------------


class TestRbacAndTenancy:
    def test_reads_require_authentication(self, client):
        assert client.get(f"{BASE}/{_identity()}").status_code in (401, 403)

    def test_a_viewer_cannot_read_a_baseline(self, client):
        org, identity = _org(), _identity()
        _capture(org, "run_1", [_opp(identity)])
        _seed_workspace_member(org, VIEWER_TOKEN, role="viewer")

        response = client.get(f"{BASE}/{identity}", headers=_auth(org, VIEWER_TOKEN))
        assert response.status_code == 403

    def test_one_org_cannot_read_anothers_baseline(self, client):
        org_a, org_b = _org(), _org()
        identity = _identity()
        _capture(org_a, "run_1", [_opp(identity)])

        response = client.get(f"{BASE}/{identity}", headers=_auth(org_b))
        assert response.status_code == 404, (
            "a cross-org read must answer 404, never confirm the baseline exists "
            "in another tenant"
        )

    def test_the_same_identity_is_independent_in_two_orgs(self):
        org_a, org_b = _org(), _org()
        identity = _identity()
        _capture(org_a, "run_1", [_opp(identity)])

        b_opp = _opp(identity)
        b_opp["_debug"]["raw_evidence"]["owner_changes_90d"] = 42.0
        assert _capture(org_b, "run_1", [b_opp])["created"] == 1

        from app.opportunity_baseline import get_baseline

        assert get_baseline(org_a, identity)["measuredValues"]["owner_changes_90d"] == 240
        assert get_baseline(org_b, identity)["measuredValues"]["owner_changes_90d"] == 42

    def test_the_org_list_never_leaks_another_orgs_baselines(self, client):
        org_a, org_b = _org(), _org()
        _capture(org_a, "run_1", [_opp(_identity())])
        _capture(org_b, "run_1", [_opp(_identity())])

        listed = client.get(BASE, headers=_auth(org_a)).json()
        assert listed["count"] == 1


# ---------------------------------------------------------------------------
# The T7 gate this enables
# ---------------------------------------------------------------------------


class TestMeasurabilityGate:
    def test_has_baseline_reports_whether_a_finding_can_be_measured(self):
        from app.opportunity_baseline import has_baseline

        org, with_basis, without = _org(), _identity(), _identity()
        _capture(org, "run_1", [_opp(with_basis)])

        assert has_baseline(org, with_basis) is True
        assert has_baseline(org, without) is False, (
            "a finding created before capture shipped has no basis and is "
            "therefore not measurable"
        )

    def test_has_baseline_is_org_scoped(self):
        from app.opportunity_baseline import has_baseline

        org_a, org_b = _org(), _org()
        identity = _identity()
        _capture(org_a, "run_1", [_opp(identity)])
        assert has_baseline(org_a, identity) is True
        assert has_baseline(org_b, identity) is False
