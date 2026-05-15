"""
test_stack_builder_launch.py — SB-13 Task 13 Sprint 7
Contract tests for POST /api/stack-builder/launch
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from typing import Dict
import os

from app.main import app


@pytest.fixture
def client():
    return TestClient(app)


def _auth() -> Dict[str, str]:
    return {"Authorization": f"Bearer {os.getenv('DEV_JWT', 'dev-token-change-me')}"}


VALID_LAUNCH = {
    "org_id": "test_org_sb13",
    "focus_id": "approvals_compliance",
    "industry_id": "public_sector",
    "template_id": "public_retirement",
    "selected_system_ids": ["salesforce_pss", "jira", "servicenow", "confluence"],
    "pack_id": "strs_benefits",
    "weightings": {
        "salesforce_pss": {
            "systemId": "salesforce_pss",
            "role": "system_of_record",
            "priority": "primary",
            "workflowFocus": ["intake_requests", "approvals", "compliance_risk"],
            "confirmed": True,
        },
        "jira": {
            "systemId": "jira",
            "role": "operational_signal_source",
            "priority": "secondary",
            "workflowFocus": ["backlog_work_queues", "change_release"],
            "confirmed": True,
        },
    },
}


class TestLaunchEndpoint:

    def test_launch_returns_200(self, client):
        resp = client.post("/api/stack-builder/launch",
            headers=_auth(), json=VALID_LAUNCH)
        assert resp.status_code == 200

    def test_launch_returns_run_id(self, client):
        resp = client.post("/api/stack-builder/launch",
            headers=_auth(), json=VALID_LAUNCH)
        data = resp.json()
        assert "runId" in data
        assert data["runId"].startswith("run_")

    def test_launch_returns_pack_id(self, client):
        resp = client.post("/api/stack-builder/launch",
            headers=_auth(), json=VALID_LAUNCH)
        assert resp.json()["packId"] == "strs_benefits"

    def test_launch_returns_focus_and_industry(self, client):
        resp = client.post("/api/stack-builder/launch",
            headers=_auth(), json=VALID_LAUNCH)
        data = resp.json()
        assert data["focusId"] == "approvals_compliance"
        assert data["industryId"] == "public_sector"

    def test_launch_returns_system_count(self, client):
        resp = client.post("/api/stack-builder/launch",
            headers=_auth(), json=VALID_LAUNCH)
        assert resp.json()["systemCount"] == 4

    def test_launch_empty_systems_400(self, client):
        body = {**VALID_LAUNCH, "selected_system_ids": []}
        resp = client.post("/api/stack-builder/launch",
            headers=_auth(), json=body)
        assert resp.status_code == 400

    def test_launch_missing_pack_id_422(self, client):
        body = {k: v for k, v in VALID_LAUNCH.items() if k != "pack_id"}
        resp = client.post("/api/stack-builder/launch",
            headers=_auth(), json=body)
        assert resp.status_code == 422

    def test_launch_null_focus_and_industry_allowed(self, client):
        body = {**VALID_LAUNCH, "focus_id": None, "industry_id": None}
        resp = client.post("/api/stack-builder/launch",
            headers=_auth(), json=body)
        assert resp.status_code == 200
        data = resp.json()
        assert data["focusId"] is None
        assert data["industryId"] is None

    def test_launch_run_stored_in_db(self, client):
        resp = client.post("/api/stack-builder/launch",
            headers=_auth(), json=VALID_LAUNCH)
        run_id = resp.json()["runId"]
        run_resp = client.get(f"/api/runs/{run_id}", headers=_auth())
        assert run_resp.status_code == 200
        run_data = run_resp.json()
        assert run_data["packId"] == "strs_benefits"
        assert run_data["source"] == "stack_builder"

    def test_launch_unique_run_ids(self, client):
        ids = set()
        for _ in range(3):
            resp = client.post("/api/stack-builder/launch",
                headers=_auth(), json=VALID_LAUNCH)
            ids.add(resp.json()["runId"])
        assert len(ids) == 3

    def test_launch_service_cloud_fallback(self, client):
        body = {**VALID_LAUNCH, "industry_id": None, "pack_id": "service_cloud"}
        resp = client.post("/api/stack-builder/launch",
            headers=_auth(), json=body)
        assert resp.status_code == 200
        assert resp.json()["packId"] == "service_cloud"
