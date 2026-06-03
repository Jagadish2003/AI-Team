"""
test_stack_builder_api.py — SB-12 Task 12 Sprint 7
Contract tests for Stack Builder API endpoints

Tests:
  Industry registry endpoints:
    GET /api/stack-builder/industries                         — 8 industries returned
    GET /api/stack-builder/industries/{id}/system-defaults    — calibrated defaults
    GET /api/stack-builder/industries/{id}/recommendations    — filtered recs
    GET /api/stack-builder/industries/unknown/system-defaults — 404

  Setup state persistence:
    POST /api/stack-builder/setup-state/{org_id}             — 204 on save
    GET  /api/stack-builder/setup-state/{org_id}             — state returned
    DELETE /api/stack-builder/setup-state/{org_id}           — 204 on delete
    GET  /api/stack-builder/setup-state/{org_id} (deleted)   — 404

  Unit tests for industry_registry.py:
    get_industry — known and unknown IDs
    get_system_defaults — known industry/system pair
    get_system_defaults — unknown industry
    get_recommended_systems — excludes selected, max 3
    get_pack_hints — correct pack ordering
    list_industries — 8 industries, correct labels
"""
from __future__ import annotations

import json
import pytest
from fastapi.testclient import TestClient
from typing import Dict
import os

from app.main import app
from discovery.packs.industry_registry import (
    get_industry,
    get_system_defaults,
    get_recommended_systems,
    get_pack_hints,
    get_llm_context_suffix,
    list_industries,
    INDUSTRY_REGISTRY,
)

# ── Test client ───────────────────────────────────────────────────────────────

@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def _auth() -> Dict[str, str]:
    return {"Authorization": f"Bearer {os.getenv('DEV_JWT', 'dev-token-change-me')}"}


# ══ Unit tests — industry_registry.py ════════════════════════════════════════

class TestIndustryRegistry:

    def test_list_industries_returns_all_eight(self):
        industries = list_industries()
        assert len(industries) == 8
        ids = {i.industry_id for i in industries}
        assert ids == {
            "financial_services", "public_sector", "logistics_supply_chain",
            "retail_commerce", "healthcare", "energy_utilities",
            "manufacturing", "technology",
        }

    def test_get_industry_known(self):
        config = get_industry("public_sector")
        assert config is not None
        assert config.label == "Public sector"
        assert "strs_benefits" in config.pack_hints

    def test_get_industry_unknown_returns_none(self):
        config = get_industry("nonexistent_industry")
        assert config is None

    def test_get_system_defaults_known_pair(self):
        defaults = get_system_defaults("public_sector", "jira")
        assert defaults is not None
        assert defaults.role == "operational_signal_source"
        assert defaults.priority == "secondary"
        assert "backlog_work_queues" in defaults.workflow_focus

    def test_get_system_defaults_financial_services_salesforce_ncino(self):
        defaults = get_system_defaults("financial_services", "salesforce_ncino")
        assert defaults is not None
        assert defaults.role == "system_of_record"
        assert defaults.priority == "primary"
        assert "compliance_risk" in defaults.workflow_focus

    def test_get_system_defaults_unknown_industry(self):
        defaults = get_system_defaults("unknown_industry", "jira")
        assert defaults is None

    def test_get_system_defaults_unknown_system(self):
        # Known industry, system not in that industry's defaults
        defaults = get_system_defaults("public_sector", "totally_unknown_system")
        assert defaults is None

    def test_get_recommended_systems_excludes_selected(self):
        # public_sector recommends: ["slack", "sharepoint", "servicenow"]
        recs = get_recommended_systems("public_sector", ["slack"])
        assert "slack" not in recs
        assert len(recs) <= 3

    def test_get_recommended_systems_max_three(self):
        recs = get_recommended_systems("financial_services", [])
        assert len(recs) <= 3

    def test_get_recommended_systems_unknown_industry(self):
        recs = get_recommended_systems("unknown_industry", [])
        assert recs == []

    def test_get_pack_hints_public_sector(self):
        hints = get_pack_hints("public_sector")
        assert "strs_benefits" in hints
        assert "service_cloud" in hints

    def test_get_pack_hints_financial_services(self):
        hints = get_pack_hints("financial_services")
        assert "ncino" in hints

    def test_get_pack_hints_unknown_industry(self):
        hints = get_pack_hints("unknown_industry")
        assert hints == []

    def test_llm_context_suffix_public_sector(self):
        suffix = get_llm_context_suffix("public_sector")
        assert "fiduciary" in suffix
        assert "Never suggest automated benefit decisions" in suffix

    def test_llm_context_suffix_financial_services(self):
        suffix = get_llm_context_suffix("financial_services")
        assert "Never suggest automated credit" in suffix

    def test_llm_context_suffix_unknown_industry(self):
        suffix = get_llm_context_suffix("unknown_industry")
        assert suffix == ""

    def test_all_workflow_focus_values_valid(self):
        """All workflow_focus values must be valid WorkflowFocusTag literals."""
        valid_tags = {
            "intake_requests", "service_casework", "approvals",
            "backlog_work_queues", "compliance_risk", "documents_knowledge",
            "handoffs_routing", "communications", "change_release", "data_analytics",
        }
        for ind_id, config in INDUSTRY_REGISTRY.items():
            for sys_id, defaults in config.system_defaults.items():
                for tag in defaults.workflow_focus:
                    assert tag in valid_tags, (
                        f"Invalid workflow_focus tag '{tag}' for "
                        f"industry='{ind_id}' system='{sys_id}'"
                    )
                assert len(defaults.workflow_focus) <= 3, (
                    f"workflow_focus max 3 violated for "
                    f"industry='{ind_id}' system='{sys_id}'"
                )

    def test_all_role_values_valid(self):
        """All role values must be valid SystemRole literals."""
        valid_roles = {
            "system_of_record", "workflow_system",
            "operational_signal_source", "documentation_system",
            "engineering_change_system",
        }
        for ind_id, config in INDUSTRY_REGISTRY.items():
            for sys_id, defaults in config.system_defaults.items():
                assert defaults.role in valid_roles, (
                    f"Invalid role '{defaults.role}' for "
                    f"industry='{ind_id}' system='{sys_id}'"
                )

    def test_all_priority_values_valid(self):
        """All priority values must be valid SystemPriority literals."""
        valid_priorities = {"primary", "secondary", "optional"}
        for ind_id, config in INDUSTRY_REGISTRY.items():
            for sys_id, defaults in config.system_defaults.items():
                assert defaults.priority in valid_priorities, (
                    f"Invalid priority '{defaults.priority}' for "
                    f"industry='{ind_id}' system='{sys_id}'"
                )


# ══ Contract tests — API endpoints ════════════════════════════════════════════

class TestIndustriesEndpoint:

    def test_list_industries_returns_200(self, client):
        resp = client.get("/api/stack-builder/industries", headers=_auth())
        assert resp.status_code == 200

    def test_list_industries_returns_eight(self, client):
        resp = client.get("/api/stack-builder/industries", headers=_auth())
        data = resp.json()
        assert len(data) == 8

    def test_list_industries_shape(self, client):
        resp = client.get("/api/stack-builder/industries", headers=_auth())
        first = resp.json()[0]
        assert "industry_id" in first
        assert "label" in first
        assert "pack_hints" in first
        assert "recommended_systems" in first

    def test_system_defaults_public_sector_jira(self, client):
        resp = client.get(
            "/api/stack-builder/industries/public_sector/system-defaults",
            headers=_auth(),
        )
        assert resp.status_code == 200
        data = resp.json()
        jira = next((d for d in data if d["system_id"] == "jira"), None)
        assert jira is not None
        assert jira["role"] == "operational_signal_source"
        assert jira["priority"] == "secondary"
        assert "backlog_work_queues" in jira["workflow_focus"]

    def test_system_defaults_unknown_industry_404(self, client):
        resp = client.get(
            "/api/stack-builder/industries/unknown_industry/system-defaults",
            headers=_auth(),
        )
        assert resp.status_code == 404

    def test_recommendations_excludes_selected(self, client):
        resp = client.get(
            "/api/stack-builder/industries/public_sector/recommendations"
            "?selected=slack,sharepoint",
            headers=_auth(),
        )
        assert resp.status_code == 200
        ids = {r["system_id"] for r in resp.json()}
        assert "slack" not in ids
        assert "sharepoint" not in ids

    def test_recommendations_max_three(self, client):
        resp = client.get(
            "/api/stack-builder/industries/financial_services/recommendations",
            headers=_auth(),
        )
        assert resp.status_code == 200
        assert len(resp.json()) <= 3

    def test_recommendations_unknown_industry_404(self, client):
        resp = client.get(
            "/api/stack-builder/industries/unknown/recommendations",
            headers=_auth(),
        )
        assert resp.status_code == 404


class TestSetupStatePersistence:

    TEST_ORG = "test_org_sb12"
    TEST_STATE = {
        "focusId": "approvals_compliance",
        "industryId": "public_sector",
        "templateId": "public_retirement",
        "selectedSystemIds": ["salesforce_pss", "jira", "servicenow", "confluence"],
        "currentStep": 3,
    }

    def test_save_setup_state_204(self, client):
        resp = client.post(
            f"/api/stack-builder/setup-state/{self.TEST_ORG}",
            headers=_auth(),
            json={"state": self.TEST_STATE, "saved_at": "2026-05-14T10:00:00Z"},
        )
        assert resp.status_code == 204

    def test_load_setup_state_returns_saved(self, client):
        # Save first
        client.post(
            f"/api/stack-builder/setup-state/{self.TEST_ORG}",
            headers=_auth(),
            json={"state": self.TEST_STATE},
        )
        # Load
        resp = client.get(
            f"/api/stack-builder/setup-state/{self.TEST_ORG}",
            headers=_auth(),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["state"]["focusId"] == "approvals_compliance"
        assert data["state"]["currentStep"] == 3
        assert "salesforce_pss" in data["state"]["selectedSystemIds"]

    def test_load_setup_state_unknown_org_404(self, client):
        resp = client.get(
            "/api/stack-builder/setup-state/nonexistent_org_xyz123",
            headers=_auth(),
        )
        assert resp.status_code == 404

    def test_delete_setup_state_204(self, client):
        # Save first
        client.post(
            f"/api/stack-builder/setup-state/{self.TEST_ORG}_delete",
            headers=_auth(),
            json={"state": self.TEST_STATE},
        )
        # Delete
        resp = client.delete(
            f"/api/stack-builder/setup-state/{self.TEST_ORG}_delete",
            headers=_auth(),
        )
        assert resp.status_code == 204

    def test_delete_then_load_returns_404(self, client):
        org = f"{self.TEST_ORG}_delete_check"
        client.post(
            f"/api/stack-builder/setup-state/{org}",
            headers=_auth(),
            json={"state": self.TEST_STATE},
        )
        client.delete(
            f"/api/stack-builder/setup-state/{org}",
            headers=_auth(),
        )
        resp = client.get(
            f"/api/stack-builder/setup-state/{org}",
            headers=_auth(),
        )
        assert resp.status_code == 404

    def test_upsert_overwrites_previous_state(self, client):
        org = f"{self.TEST_ORG}_upsert"
        # Save initial
        client.post(
            f"/api/stack-builder/setup-state/{org}",
            headers=_auth(),
            json={"state": self.TEST_STATE},
        )
        # Overwrite
        updated = {**self.TEST_STATE, "currentStep": 4, "focusId": "core_operations"}
        client.post(
            f"/api/stack-builder/setup-state/{org}",
            headers=_auth(),
            json={"state": updated},
        )
        # Verify
        resp = client.get(
            f"/api/stack-builder/setup-state/{org}",
            headers=_auth(),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["state"]["currentStep"] == 4
        assert data["state"]["focusId"] == "core_operations"

    def test_delete_idempotent(self, client):
        """DELETE on non-existent org returns 204, not 404."""
        resp = client.delete(
            "/api/stack-builder/setup-state/absolutely_nonexistent_org",
            headers=_auth(),
        )
        assert resp.status_code == 204
