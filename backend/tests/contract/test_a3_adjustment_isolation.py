"""Contract tests for 2.0-A3 T6 - learning adjustment isolation.

T6 is the tenancy and boundary story for the learned ranking layer:

* one org's decisions and measured outcomes must never affect another org;
* cross-org reads must look like not-found, not "exists elsewhere";
* reset must neutralise exactly one org and leave another org's state/history
  intact;
* learning configuration is explicitly deployment-wide unless a future release
  moves it into an org-scoped store.
"""

from __future__ import annotations

import json
import os
from contextlib import closing
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app

DEV_TOKEN = os.getenv("DEV_JWT", "dev-token-change-me")
BASE = "/api/learning/adjustment"


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture(autouse=True)
def _tables() -> None:
    from app.learning_adjustment_state import ensure_ranking_adjustment_tables
    from app.learning_feedback import ensure_opportunity_feedback_table
    from app.opportunity_lifecycle import ensure_opportunity_lifecycle_tables
    from app.opportunity_movement import ensure_opportunity_movement_table

    ensure_opportunity_feedback_table()
    ensure_ranking_adjustment_tables()
    ensure_opportunity_lifecycle_tables()
    ensure_opportunity_movement_table()


def _auth(org_id: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {DEV_TOKEN}", "X-Org-Id": org_id}


def _seed_workspace_member(org_id: str, user_id: str = DEV_TOKEN) -> None:
    from app.rbac import _ensure_members_table

    _ensure_members_table()
    with closing(db.connect()) as con:
        with con.cursor() as cur:
            cur.execute(
                "INSERT INTO workspace_members (org_id, user_id, role, created_at) "
                "VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (org_id, user_id) "
                "DO UPDATE SET role = EXCLUDED.role, is_deleted = FALSE",
                (org_id, user_id, "owner", datetime.now(timezone.utc).isoformat()),
            )
        con.commit()


def _org() -> str:
    org_id = f"org-a3t6-{uuid4().hex[:8]}"
    _seed_workspace_member(org_id)
    return org_id


def _identity() -> str:
    return f"opp_{uuid4().hex[:24]}"


def _feedback(
    org: str,
    action: str,
    detector: str,
    *,
    identity: str | None = None,
) -> Dict[str, Any]:
    from app.learning_feedback import record_feedback

    return record_feedback(
        org,
        identity or _identity(),
        action,
        actor_id="analyst_1",
        detector_id=detector,
        pack_id="service_cloud",
    )


def _seed_movement(
    org: str,
    detector: str,
    *,
    verdict: str,
    direction: str,
    identity: str | None = None,
) -> None:
    from app.opportunity_lifecycle import ensure_tracked, record_action

    opp_identity = identity or _identity()
    now = datetime.now(timezone.utc)
    action_date = (now - timedelta(days=30)).date()

    ensure_tracked(org, opp_identity, run_id="run_base")
    record_action(org, opp_identity, action_date.isoformat(), "analyst_1")
    record = {
        "schemaVersion": "1.0.0",
        "orgId": org,
        "opportunityIdentity": opp_identity,
        "detectorId": detector,
        "actionDate": action_date.isoformat(),
        "baselineRunId": "run_base",
        "currentRunId": "run_now",
        "movements": [
            {
                "signalName": "owner_changes_90d",
                "role": "movement",
                "direction": direction,
            }
        ],
        "comparability": {"verdict": "comparable"},
        "confounderSummary": {"count": 0, "materialCount": 0, "advisoryCount": 0},
        "projectionValidation": {
            "verdict": verdict,
            "projected": {"packId": "service_cloud", "packVersion": "1.2.0"},
        },
        "measuredAt": now.isoformat(),
    }
    with closing(db.connect()) as con:
        with con.cursor() as cur:
            cur.execute(
                "INSERT INTO opportunity_movements ("
                "  org_id, opportunity_identity, current_run_id, baseline_run_id,"
                "  detector_id, action_date, comparability_verdict, record,"
                "  measured_at, created_at, updated_at,"
                "  projection_validation_verdict"
                ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (org_id, opportunity_identity, current_run_id)"
                " DO NOTHING",
                (
                    org,
                    opp_identity,
                    "run_now",
                    "run_base",
                    detector,
                    action_date,
                    "comparable",
                    json.dumps(record),
                    now,
                    now,
                    now,
                    verdict,
                ),
            )
        con.commit()


def _seed_strong_opposing_learning(org: str) -> None:
    for _ in range(12):
        _feedback(org, "accept", "FAVOURED")
        _feedback(org, "dismiss", "DISFAVOURED")
    for _ in range(4):
        _seed_movement(
            org,
            "FAVOURED",
            verdict="within_band",
            direction="improved",
        )
        _seed_movement(
            org,
            "DISFAVOURED",
            verdict="below_band",
            direction="worsened",
        )


def _opp(index: int, detector: str) -> Dict[str, Any]:
    return {
        "id": f"opp_{index:03d}",
        "opportunity_identity": f"ident_{index:03d}",
        "title": f"Finding {index}",
        "category": "Workflow",
        "tier": "Quick Win",
        "impact": 7,
        "effort": 3,
        "confidence": "HIGH",
        "aiRationale": "seeded",
        "evidenceIds": [f"ev_{index}"],
        "corroboration_sources": ["servicenow", "jira"],
        "corroboration_label": "Corroborated",
        "decision": "UNREVIEWED",
        "override": {
            "isLocked": False,
            "rationaleOverride": "",
            "overrideReason": "",
            "updatedAt": None,
        },
        "packId": "service_cloud",
        "_debug": {"detector_id": detector},
    }


def _opps() -> List[Dict[str, Any]]:
    return [_opp(i, "DISFAVOURED") for i in range(5)] + [
        _opp(i + 5, "FAVOURED") for i in range(5)
    ]


def _run_with_opps(opps: List[Dict[str, Any]]) -> str:
    from app.db import run_kv_set
    from app.run_store import start_run_

    run_id = start_run_({"pack": "service_cloud"})["runId"]
    run_kv_set("opps", run_id, opps)
    return run_id


def _ids(rows: List[Dict[str, Any]]) -> List[str]:
    return [row["id"] for row in rows]


class TestAdversarialTwoOrgIsolation:
    def test_org_a_feedback_and_outcomes_never_reorder_org_b(self, client):
        org_a, org_b = _org(), _org()
        _seed_strong_opposing_learning(org_a)

        recompute_a = client.post(f"{BASE}/recompute", headers=_auth(org_a)).json()
        recompute_b = client.post(f"{BASE}/recompute", headers=_auth(org_b)).json()
        assert recompute_a["learningActive"] is True
        assert recompute_b["learningActive"] is False
        assert client.get(BASE, headers=_auth(org_b)).json()["groups"] == []

        opps = _opps()
        run_id = _run_with_opps(opps)
        served_a = client.get(
            f"/api/runs/{run_id}/opportunities", headers=_auth(org_a)
        ).json()
        served_b = client.get(
            f"/api/runs/{run_id}/opportunities", headers=_auth(org_b)
        ).json()

        assert _ids(served_a) != _ids(opps), (
            "org A seed did not move its own ranking, so the leak test is weak"
        )
        assert _ids(served_b) == _ids(opps), (
            "org A's learned state reordered org B's base list"
        )
        assert all("_ranking" not in row for row in served_b)

        preview_b = client.get(f"{BASE}/preview/{run_id}", headers=_auth(org_b)).json()
        assert preview_b["learningActive"] is False
        assert "not yet active" in (preview_b["inactiveReason"] or "")

    def test_cross_org_feedback_entry_is_not_found(self, client):
        org_a, org_b = _org(), _org()
        feedback = _feedback(org_a, "accept", "FAVOURED")

        response = client.get(
            f"/api/learning/feedback/entry/{feedback['feedbackId']}",
            headers=_auth(org_b),
        )
        assert response.status_code == 404


class TestResetIsOrgScoped:
    def test_reset_org_a_leaves_org_b_state_history_and_ranking_intact(self, client):
        org_a, org_b = _org(), _org()
        _seed_strong_opposing_learning(org_a)
        _seed_strong_opposing_learning(org_b)
        client.post(f"{BASE}/recompute", headers=_auth(org_a))
        client.post(f"{BASE}/recompute", headers=_auth(org_b))

        b_state_before = client.get(BASE, headers=_auth(org_b)).json()["groups"]
        b_history_before = client.get(f"{BASE}/history", headers=_auth(org_b)).json()
        assert any(group["learningActive"] for group in b_state_before)
        assert b_history_before

        reset_a = client.post(
            f"{BASE}/reset",
            headers=_auth(org_a),
            json={"reason": "T6 isolation regression"},
        )
        assert reset_a.status_code == 200, reset_a.text

        a_state_after = client.get(BASE, headers=_auth(org_a)).json()["groups"]
        assert a_state_after
        assert all(group["learningActive"] is False for group in a_state_after)
        assert all(group["netWeight"] == 0 for group in a_state_after)

        b_state_after = client.get(BASE, headers=_auth(org_b)).json()["groups"]
        b_history_after = client.get(f"{BASE}/history", headers=_auth(org_b)).json()
        assert b_state_after == b_state_before
        assert b_history_after == b_history_before
        assert all(row["changeKind"] != "reset" for row in b_history_after)

        opps = _opps()
        run_id = _run_with_opps(opps)
        served_b = client.get(
            f"/api/runs/{run_id}/opportunities", headers=_auth(org_b)
        ).json()
        assert _ids(served_b) != _ids(opps), "org B's active ranking was reset by org A"


class TestLearningConfigScope:
    def test_learning_config_declares_deployment_wide_scope(self, client):
        body = client.get("/api/learning/config", headers=_auth(_org())).json()
        assert body["configurationScope"] == "deployment_wide"
        assert body["coldStart"]["basis"] == "provisional"
