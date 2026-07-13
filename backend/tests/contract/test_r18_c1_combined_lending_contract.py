"""R18-C1 T6 - combined Lending Template / registry contract coverage.

These tests exercise the public registry and launch APIs as one contract.  They
deliberately avoid calling ``resolve_launch_config`` directly: the behavior under
test is what Stack Builder clients receive and what downstream run surfaces use.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Dict

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app
from discovery.packs.industry_registry import (
    INDUSTRY_REGISTRY,
    IndustryConfig,
    SystemDefaultConfig,
)
from discovery.packs.template_registry import (
    FocusDefaults,
    TemplateDefinition,
    register_template,
    unregister_template,
)


def _auth() -> Dict[str, str]:
    token = os.getenv("DEV_JWT", "dev-token-change-me")
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def registry_fixtures():
    """Add an industry and template through configuration only."""
    industry = IndustryConfig(
        industry_id="insurance_contract_fixture",
        label="Insurance contract fixture",
        pack_hints=["service_cloud"],
        system_defaults={
            "salesforce_sc": SystemDefaultConfig(
                "system_of_record", "primary", ["service_casework"]
            )
        },
        recommended_systems=["salesforce_sc"],
        llm_context_suffix="Contract fixture only.",
    )
    template = TemplateDefinition(
        template_id="insurance_contract_fixture",
        label="Insurance template fixture",
        description="Configuration-only contract fixture.",
        suggested_systems=["salesforce_sc"],
        suggested_roles={"salesforce_sc": "system_of_record"},
        focus_defaults=FocusDefaults(
            focus_id="member_customer_service", emphasis=["service_casework"]
        ),
        pack_id="service_cloud",
        terminology={"customer": "policyholder"},
        metadata={"industry_id": industry.industry_id, "source": "contract_fixture"},
    )

    INDUSTRY_REGISTRY[industry.industry_id] = industry
    register_template(template)
    try:
        yield industry, template
    finally:
        INDUSTRY_REGISTRY.pop(industry.industry_id, None)
        unregister_template(template.template_id)


def test_registry_contract_exposes_lending_bundle_and_config_only_fixtures(
    client, registry_fixtures
):
    """AC1/AC4/AC7/AC8: both pickers and all lending defaults are server-owned."""
    industry, template = registry_fixtures

    industries_response = client.get(
        "/api/stack-builder/industries", headers=_auth()
    )
    templates_response = client.get(
        "/api/stack-builder/templates", headers=_auth()
    )
    defaults_response = client.get(
        "/api/stack-builder/industries/financial_services/system-defaults",
        headers=_auth(),
    )

    assert industries_response.status_code == 200
    assert templates_response.status_code == 200
    assert defaults_response.status_code == 200

    industries = {item["industry_id"]: item for item in industries_response.json()}
    templates = {item["template_id"]: item for item in templates_response.json()}
    system_defaults = {
        item["system_id"]: item for item in defaults_response.json()
    }

    # A new entry appears through the existing endpoints without route/UI code.
    assert industries[industry.industry_id]["label"] == industry.label
    assert templates[template.template_id]["label"] == template.label
    assert templates[template.template_id]["terminology"]["customer"] == "policyholder"

    lending = templates["commercial_lending"]
    assert lending["suggested_systems"] == [
        "salesforce_ncino",
        "jira",
        "servicenow",
        "slack",
        "teams",
        "confluence",
    ]
    assert lending["suggested_roles"] == {
        "salesforce_ncino": "system_of_record",
        "jira": "workflow_system",
        "servicenow": "workflow_system",
        "slack": "operational_signal_source",
        "teams": "operational_signal_source",
        "confluence": "documentation_system",
    }
    assert lending["focus_defaults"] == {
        "focus_id": "approvals_compliance",
        "emphasis": ["approvals", "compliance_risk", "backlog_work_queues"],
    }
    assert lending["pack_id"] == "ncino"
    assert set(lending["detector_emphasis"]) == {
        "COVENANT_TRACKING_GAP",
        "APPROVAL_BOTTLENECK",
        "CHECKLIST_BOTTLENECK",
        "SPREADING_BOTTLENECK",
        "LOAN_ORIGINATION_ROUTING_FRICTION",
    }
    assert lending["terminology"] == {
        "customer": "borrower",
        "account": "facility",
        "obligation": "covenant",
        "rationale": "credit memo",
        "approval": "approval gate",
    }

    # Industry selection receives calibrated backend defaults, not a UI map.
    assert system_defaults["salesforce_ncino"] == {
        "system_id": "salesforce_ncino",
        "role": "system_of_record",
        "priority": "primary",
        "workflow_focus": ["approvals", "compliance_risk", "handoffs_routing"],
    }


def _launch(client: TestClient, body: dict) -> dict:
    response = client.post(
        "/api/stack-builder/launch", headers=_auth(), json=body
    )
    assert response.status_code == 200, response.text
    run_id = response.json()["runId"]
    run_response = client.get(f"/api/runs/{run_id}", headers=_auth())
    assert run_response.status_code == 200, run_response.text
    return run_response.json()


def test_lending_defaults_are_editable_and_run_provenance_preserves_effective_state(
    client,
):
    """AC1/AC2/AC5: untouched defaults and user edits survive the API boundary."""
    untouched = _launch(
        client,
        {
            "org_id": "r18_c1_t6",
            "industry_id": "financial_services",
            "template_id": "commercial_lending",
        },
    )
    assert untouched["packId"] == "ncino"
    assert untouched["focusId"] == "approvals_compliance"
    assert untouched["selectedSystemIds"] == [
        "salesforce_ncino",
        "jira",
        "servicenow",
        "slack",
        "teams",
        "confluence",
    ]
    assert untouched["templateProvenance"]["untouched"] is True
    assert untouched["templateProvenance"]["edited_fields"] == []
    assert "COVENANT_TRACKING_GAP" in untouched["templateProvenance"][
        "template_defaults"
    ]["detector_emphasis"]
    assert untouched["templateProvenance"]["template_defaults"][
        "focus_emphasis"
    ] == ["approvals", "compliance_risk", "backlog_work_queues"]
    assert untouched["templateProvenance"]["template_defaults"]["terminology"][
        "customer"
    ] == "borrower"

    untouched_context = db.run_kv_get("setup_context", untouched["id"])
    assert untouched_context["weightings"]["salesforce_ncino"]["role"] == (
        "system_of_record"
    )
    assert untouched_context["weightings"]["jira"]["role"] == "workflow_system"
    assert untouched["weightings"] == untouched_context["weightings"]

    edited_body = {
        "org_id": "r18_c1_t6",
        "industry_id": "financial_services",
        "template_id": "commercial_lending",
        "pack_id": "service_cloud",
        "focus_id": "core_operations",
        "selected_system_ids": ["salesforce_ncino", "confluence", "slack"],
        "weightings": {
            "salesforce_ncino": {
                "systemId": "salesforce_ncino",
                "role": "documentation_system",
                "priority": "secondary",
                "workflowFocus": ["documents_knowledge"],
                "confirmed": True,
            },
            "slack": {
                "systemId": "slack",
                "role": "operational_signal_source",
                "priority": "secondary",
                "workflowFocus": ["communications"],
                "confirmed": True,
            },
        },
    }
    edited = _launch(client, edited_body)

    assert edited["templateId"] == "commercial_lending"
    assert edited["packId"] == "service_cloud"
    assert edited["focusId"] == "core_operations"
    assert edited["selectedSystemIds"] == edited_body["selected_system_ids"]
    assert edited["templateProvenance"]["untouched"] is False
    assert set(edited["templateProvenance"]["edited_fields"]) == {
        "pack_id",
        "focus_id",
        "selected_system_ids",
        "roles",
    }

    setup_context = db.run_kv_get("setup_context", edited["id"])
    assert setup_context["template_id"] == "commercial_lending"
    assert setup_context["selected_system_ids"] == edited_body["selected_system_ids"]
    assert setup_context["weightings"]["salesforce_ncino"] == edited_body[
        "weightings"
    ]["salesforce_ncino"]
    assert setup_context["weightings"]["slack"] == edited_body["weightings"]["slack"]
    assert setup_context["weightings"]["confluence"]["role"] == (
        "documentation_system"
    )
    assert edited["weightings"] == setup_context["weightings"]
    assert setup_context["template_provenance"] == edited["templateProvenance"]


_GENERIC_OPPORTUNITY = {
    "id": "opp_r18_c1_t6",
    "title": "Customer approval backlog",
    "category": "Approval bottleneck",
    "description": (
        "Accounts await customer approval while obligations and rationale are missing."
    ),
    "aiRationale": "Customer approval is blocked across accounts and obligations.",
    "detector_id": "APPROVAL_BOTTLENECK",
    "tier": "Quick Win",
    "impact": 5,
    "effort": 3,
    "confidence": "High",
    "decision": "UNREVIEWED",
    "evidenceIds": [],
    "requiredPermissions": [],
}


def test_template_launch_provenance_drives_lending_output_terminology(client):
    """AC3/AC5: the real launched template, not a test-only flag, shapes output."""
    run = _launch(
        client,
        {
            "org_id": "r18_c1_t6_terms",
            "template_id": "commercial_lending",
        },
    )
    run_id = run["id"]
    db.run_kv_set("opps", run_id, [dict(_GENERIC_OPPORTUNITY)])
    db.run_kv_set("executive_report", run_id, None)

    opportunities = client.get(
        f"/api/runs/{run_id}/opportunities", headers=_auth()
    )
    report = client.get(
        f"/api/runs/{run_id}/executive-report", headers=_auth()
    )
    assert opportunities.status_code == 200
    assert report.status_code == 200

    finding_blob = str(opportunities.json()).lower()
    report_blob = str(report.json()).lower()
    for term in ("borrower", "facilities", "covenant", "credit memo", "approval gate"):
        assert term in finding_blob
    assert "borrower" in report_blob
    assert "approval gate" in report_blob
    assert opportunities.json()[0]["detector_id"] == "APPROVAL_BOTTLENECK"


def test_stack_builder_runtime_has_no_hardcoded_industry_or_template_arrays():
    """AC10 regression guard: runtime UI must consume registry responses."""
    repo_root = Path(__file__).resolve().parents[3]
    frontend_src = repo_root / "frontend" / "src"
    forbidden = re.compile(
        r"\b(?:const|let|var)\s+(?:"
        r"INDUSTRIES|TEMPLATES|INDUSTRY_PACK_HINTS|"
        r"INDUSTRY_LABELS|TEMPLATE_LABELS)\b"
    )

    offenders = []
    for source in frontend_src.rglob("*.ts*"):
        if "__tests__" in source.parts:
            continue
        if forbidden.search(source.read_text(encoding="utf-8")):
            offenders.append(str(source.relative_to(repo_root)))

    assert offenders == [], (
        "Industry/template runtime arrays reintroduced; use the backend registry: "
        + ", ".join(offenders)
    )

    stack_builder_source = (
        frontend_src / "pages" / "StackBuilderPage.tsx"
    ).read_text(encoding="utf-8")
    assert "fetchIndustries" in stack_builder_source
    assert "fetchTemplates" in stack_builder_source
