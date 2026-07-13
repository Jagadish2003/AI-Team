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
    with TestClient(app) as c:
        yield c


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


# ── R18-C1 T2 — Commercial Lending template instance ──────────────────────────

class TestCommercialLendingTemplateLaunch:
    """
    AC1: selecting the template pre-populates systems/roles/focus/pack, editable.
    AC2: an UNTOUCHED template launch applies the lending pack + focus, and the
         template settings are visible in the run record.
    AC5: template selection + which user edits were made are recorded on the run.
    """

    # An "untouched" lending launch: the caller selects the template and sends
    # nothing else (no pack/focus/systems). The template supplies the defaults.
    UNTOUCHED_LENDING = {
        "org_id": "test_org_lending",
        "template_id": "commercial_lending",
    }

    def test_untouched_template_launch_returns_200(self, client):
        resp = client.post(
            "/api/stack-builder/launch", headers=_auth(), json=self.UNTOUCHED_LENDING
        )
        assert resp.status_code == 200

    def test_untouched_template_applies_lending_pack_and_focus(self, client):
        """AC2: the untouched lending template drives the ncino pack + focus."""
        resp = client.post(
            "/api/stack-builder/launch", headers=_auth(), json=self.UNTOUCHED_LENDING
        )
        data = resp.json()
        assert data["packId"] == "ncino"
        assert data["focusId"] == "approvals_compliance"
        assert data["systemCount"] == 4  # the template's 4 suggested systems

    def test_untouched_template_settings_visible_in_run_record(self, client):
        """AC2: the run record shows the template and its resolved settings."""
        resp = client.post(
            "/api/stack-builder/launch", headers=_auth(), json=self.UNTOUCHED_LENDING
        )
        run_id = resp.json()["runId"]
        run = client.get(f"/api/runs/{run_id}", headers=_auth()).json()

        assert run["templateId"] == "commercial_lending"
        assert run["packId"] == "ncino"
        assert run["focusId"] == "approvals_compliance"
        assert "salesforce_ncino" in run["selectedSystemIds"]

        prov = run["templateProvenance"]
        assert prov["applied"] is True
        assert prov["template_id"] == "commercial_lending"
        # Untouched: no user edits recorded.
        assert prov["untouched"] is True
        assert prov["edited_fields"] == []
        # The template defaults snapshot is preserved for provenance, including
        # the lending detector emphasis.
        defaults = prov["template_defaults"]
        assert defaults["pack_id"] == "ncino"
        assert "COVENANT_TRACKING_GAP" in defaults["detector_emphasis"]
        assert "APPROVAL_BOTTLENECK" in defaults["detector_emphasis"]

    def test_template_edits_are_recorded_as_provenance(self, client):
        """AC5: user edits vs. the template defaults are recorded on the run."""
        edited = {
            "org_id": "test_org_lending",
            "template_id": "commercial_lending",
            # Edit the pack away from the lending default and change a role.
            "pack_id": "service_cloud",
            "focus_id": "approvals_compliance",  # unchanged (matches default)
            "selected_system_ids": ["salesforce_ncino", "jira", "servicenow", "confluence"],
            "weightings": {
                "jira": {"systemId": "jira", "role": "documentation_system", "confirmed": True},
            },
        }
        resp = client.post("/api/stack-builder/launch", headers=_auth(), json=edited)
        assert resp.status_code == 200
        run_id = resp.json()["runId"]
        run = client.get(f"/api/runs/{run_id}", headers=_auth()).json()

        # The user's edits win (editable defaults, AC1).
        assert run["packId"] == "service_cloud"

        prov = run["templateProvenance"]
        assert prov["template_id"] == "commercial_lending"
        assert prov["untouched"] is False
        # Exactly the changed fields are recorded; the unchanged focus is not.
        assert set(prov["edited_fields"]) == {"pack_id", "roles"}

    def test_template_can_launch_without_explicit_pack(self, client):
        """A known template supplies the pack, so pack_id is not required (AC2)."""
        body = {"org_id": "test_org_lending", "template_id": "commercial_lending"}
        resp = client.post("/api/stack-builder/launch", headers=_auth(), json=body)
        assert resp.status_code == 200
        assert resp.json()["packId"] == "ncino"

    def test_no_template_and_no_pack_still_422(self, client):
        """Backward compatible: no template and no pack is still invalid."""
        body = {"org_id": "test_org", "selected_system_ids": ["salesforce_sc"]}
        resp = client.post("/api/stack-builder/launch", headers=_auth(), json=body)
        assert resp.status_code == 422
