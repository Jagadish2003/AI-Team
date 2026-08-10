"""Contract tests for 2.0-A3 T4 - reset and audit governance.

T4 makes the learned ranking layer governable: an Owner can inspect current
state, read append-only history, and reset the org's adjustment layer to neutral.
Every state change is present in the audit log, with enough before/after detail
to reconstruct what changed.
"""

from __future__ import annotations

import json
import os
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app

DEV_TOKEN = os.getenv("DEV_JWT", "dev-token-change-me")
VIEWER_TOKEN = os.getenv("VIEWER_JWT", "viewer-token")
BASE = "/api/learning/adjustment"
EVENT = "ranking_adjustment_changed"


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture(autouse=True)
def _tables() -> None:
    from app.learning_adjustment_state import ensure_ranking_adjustment_tables
    from app.learning_feedback import ensure_opportunity_feedback_table
    from database.models.audit_log import (
        CREATE_AUDIT_LOG_IDX_ORG_EVENT,
        CREATE_AUDIT_LOG_IDX_ORG_TS,
        CREATE_AUDIT_LOG_TABLE,
    )

    ensure_opportunity_feedback_table()
    ensure_ranking_adjustment_tables()
    with closing(db.connect()) as con:
        with con.cursor() as cur:
            cur.execute(CREATE_AUDIT_LOG_TABLE)
            cur.execute(CREATE_AUDIT_LOG_IDX_ORG_TS)
            cur.execute(CREATE_AUDIT_LOG_IDX_ORG_EVENT)
        con.commit()


def _auth(org_id: str, token: str = DEV_TOKEN) -> Dict[str, str]:
    return {"Authorization": f"Bearer {token}", "X-Org-Id": org_id}


def _seed_workspace_member(org_id: str, user_id: str, role: str = "owner") -> None:
    from app.rbac import _ensure_members_table

    _ensure_members_table()
    with closing(db.connect()) as con:
        with con.cursor() as cur:
            cur.execute(
                "INSERT INTO workspace_members (org_id, user_id, role, created_at) "
                "VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (org_id, user_id) "
                "DO UPDATE SET role = EXCLUDED.role, is_deleted = FALSE",
                (org_id, user_id, role, datetime.now(timezone.utc).isoformat()),
            )
        con.commit()


def _org() -> str:
    org_id = f"org-a3t4-{uuid4().hex[:8]}"
    _seed_workspace_member(org_id, DEV_TOKEN, "owner")
    _seed_workspace_member(org_id, VIEWER_TOKEN, "viewer")
    return org_id


def _identity() -> str:
    return f"opp_{uuid4().hex[:24]}"


def _feedback(org: str, action: str, detector: str) -> None:
    from app.learning_feedback import record_feedback

    record_feedback(
        org,
        _identity(),
        action,
        actor_id="analyst_1",
        detector_id=detector,
        pack_id="service_cloud",
    )


def _seed_opposing_state(client: TestClient, org: str) -> None:
    for _ in range(8):
        _feedback(org, "accept", "FAVOURED")
        _feedback(org, "dismiss", "DISFAVOURED")
    response = client.post(f"{BASE}/recompute", headers=_auth(org))
    assert response.status_code == 200, response.text


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


def _run_with_opps(opps: List[Dict[str, Any]]) -> str:
    from app.db import run_kv_set
    from app.run_store import start_run_

    run_id = start_run_({"pack": "service_cloud"})["runId"]
    run_kv_set("opps", run_id, opps)
    return run_id


def _audit_rows(org_id: str) -> List[Dict[str, Any]]:
    with closing(db.connect()) as con:
        with con.cursor() as cur:
            cur.execute(
                "SELECT event_type, user_id, payload, timestamp FROM audit_log "
                "WHERE org_id = %s AND event_type = %s ORDER BY timestamp ASC",
                (org_id, EVENT),
            )
            rows = cur.fetchall()
    out = []
    for event_type, user_id, payload, timestamp in rows:
        out.append(
            {
                "event_type": event_type,
                "user_id": user_id,
                "payload": json.loads(payload) if payload else {},
                "timestamp": timestamp,
            }
        )
    return out


class TestOwnerGovernanceSurface:
    def test_state_history_and_reset_are_owner_only(self, client):
        org = _org()

        assert client.get(BASE, headers=_auth(org, VIEWER_TOKEN)).status_code == 403
        assert (
            client.get(f"{BASE}/history", headers=_auth(org, VIEWER_TOKEN)).status_code
            == 403
        )
        assert (
            client.post(
                f"{BASE}/reset",
                headers=_auth(org, VIEWER_TOKEN),
                json={"reason": "viewer must not reset"},
            ).status_code
            == 403
        )

        assert client.get(BASE, headers=_auth(org)).status_code == 200
        assert client.get(f"{BASE}/history", headers=_auth(org)).status_code == 200

    def test_the_governance_routes_are_structurally_owner_gated(self):
        backend = Path(__file__).resolve().parents[2]
        source = (backend / "app" / "routes_learning_adjustment.py").read_text(
            encoding="utf-8"
        )
        for route in (
            '@router.get("",',
            '@router.get("/history"',
            '@router.post("/reset"',
        ):
            index = source.index(route)
            snippet = source[index:index + 140]
            assert 'require_role("owner")' in snippet


class TestResetNeutralisesTheCurrentState:
    def test_reset_refuses_a_missing_or_blank_audit_reason(self, client):
        org = _org()
        assert client.post(f"{BASE}/reset", headers=_auth(org)).status_code == 422
        blank = client.post(
            f"{BASE}/reset", headers=_auth(org), json={"reason": "   "}
        )
        assert blank.status_code == 400

    def test_reset_returns_state_to_neutral_and_appends_history(self, client):
        org = _org()
        _seed_opposing_state(client, org)

        before = client.get(BASE, headers=_auth(org)).json()
        assert any(group["learningActive"] for group in before["groups"])
        assert any(group["netWeight"] for group in before["groups"])

        reset = client.post(
            f"{BASE}/reset",
            headers=_auth(org),
            json={"reason": "owner requested neutral ranking"},
        )
        assert reset.status_code == 200, reset.text
        body = reset.json()
        assert body["changeKind"] == "reset"
        assert body["groupsReset"] == len(before["groups"])
        assert body["opportunitiesAffected"] > 0
        assert body["previousState"] == before["groups"]
        assert body["reason"] == "owner requested neutral ranking"

        after = client.get(BASE, headers=_auth(org)).json()
        assert after["groups"], "reset should leave inspectable neutral rows"
        assert all(group["netWeight"] == 0 for group in after["groups"])
        assert all(group["learningActive"] is False for group in after["groups"])

        history = client.get(f"{BASE}/history", headers=_auth(org)).json()
        reset_history = [row for row in history if row["changeKind"] == "reset"]
        assert reset_history
        assert all(
            row["resetReason"] == "owner requested neutral ranking"
            for row in reset_history
        )
        assert any(row["changeKind"] in {"activated", "recomputed"} for row in history)

        with closing(db.connect()) as con:
            with con.cursor() as cur:
                cur.execute(
                    "SELECT DISTINCT reset_reason FROM ranking_adjustment_history "
                    "WHERE org_id = %s AND change_kind = 'reset'",
                    (org,),
                )
                stored_reasons = {row[0] for row in cur.fetchall()}
        assert stored_reasons == {"owner requested neutral ranking"}

    def test_reset_restores_served_rankings_to_base_order(self, client):
        org = _org()
        _seed_opposing_state(client, org)

        opps = [_opp(i, "DISFAVOURED") for i in range(5)]
        opps += [_opp(i + 5, "FAVOURED") for i in range(5)]
        run_id = _run_with_opps(opps)

        adjusted = client.get(
            f"/api/runs/{run_id}/opportunities", headers=_auth(org)
        ).json()
        assert [item["id"] for item in adjusted] != [item["id"] for item in opps]

        client.post(
            f"{BASE}/reset",
            headers=_auth(org),
            json={"reason": "restore base order"},
        )
        neutral = client.get(
            f"/api/runs/{run_id}/opportunities", headers=_auth(org)
        ).json()
        assert [item["id"] for item in neutral] == [item["id"] for item in opps]


class TestAuditAndTelemetryRegistration:
    def test_recompute_and_reset_both_write_audit_events(self, client):
        org = _org()
        _seed_opposing_state(client, org)
        client.post(
            f"{BASE}/reset",
            headers=_auth(org),
            json={"reason": "audit reset"},
        )

        rows = _audit_rows(org)
        kinds = [row["payload"]["change_kind"] for row in rows]
        assert "activated" in kinds
        assert "reset" in kinds

        activation = next(
            row for row in rows if row["payload"]["change_kind"] == "activated"
        )
        assert activation["payload"]["previous_state"] == []
        assert activation["payload"]["current_state"]

        reset = next(row for row in rows if row["payload"]["change_kind"] == "reset")
        assert reset["event_type"] == EVENT
        assert reset["user_id"] == DEV_TOKEN
        assert reset["timestamp"]
        assert reset["payload"]["target"] == "ranking_adjustments"
        assert reset["payload"]["previous_state"]
        assert reset["payload"]["current_state"]
        assert reset["payload"]["opportunities_affected"] > 0

    def test_event_types_are_registered_before_emission(self):
        from app.middleware.audit import (
            AUDIT_EVENT_REGISTRY,
            RANKING_ADJUSTMENT_CHANGED,
        )
        from app.telemetry import REGISTERED_EVENT_TYPES
        from app.ranking_adjustment_audit import TELEMETRY_RANKING_ADJUSTMENT_CHANGED

        assert RANKING_ADJUSTMENT_CHANGED in AUDIT_EVENT_REGISTRY
        assert TELEMETRY_RANKING_ADJUSTMENT_CHANGED in REGISTERED_EVENT_TYPES
