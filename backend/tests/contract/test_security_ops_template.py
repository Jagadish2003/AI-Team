"""MSP-B12 T5 — Security Operations template and combined launch contract."""
from __future__ import annotations

import os
from dataclasses import fields
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app
from app.pack_aware_enrichment import run_pack_aware_enrichment
from app.terminology import apply_run_terminology
from discovery.packs.pack_config import get_pack_version
from discovery.runner import run
from discovery.packs.template_registry import (
    TemplateDefinition,
    get_template,
    resolve_launch_config,
)


SECURITY_TEMPLATE_ID = "security_operations"
CLOUD_TEMPLATE_ID = "managed_cloud_operations"


def _auth() -> Dict[str, str]:
    return {"Authorization": f"Bearer {os.getenv('DEV_JWT', 'dev-token-change-me')}"}


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def test_generic_template_model_is_unchanged():
    assert [field.name for field in fields(TemplateDefinition)] == [
        "template_id",
        "label",
        "description",
        "suggested_systems",
        "suggested_roles",
        "focus_defaults",
        "pack_id",
        "detector_emphasis",
        "terminology",
        "metadata",
    ]


def test_security_operations_registry_defaults_match_document():
    template = get_template(SECURITY_TEMPLATE_ID)
    assert template is not None
    assert template.label == "Security Operations"
    assert template.pack_id == "security_ops"
    assert template.focus_defaults.focus_id == "core_operations"
    assert template.suggested_systems == [
        "servicenow",
        "aws_event_source",
        "azure_event_source",
        "runbook_library",
    ]
    assert template.suggested_roles == {
        "servicenow": "system_of_record",
        "aws_event_source": "operational_signal_source",
        "azure_event_source": "operational_signal_source",
        "runbook_library": "documentation_system",
    }
    assert set(template.detector_emphasis) == {
        "SECOPS_REMEDIATION_RECURRENCE",
        "SECOPS_SECURITY_IT_PING_PONG",
        "SECOPS_SLA_DEFERRAL_AGEING",
        "SECOPS_SHARED_INFRA_CONCENTRATION",
        "SECOPS_SIR_TRIAGE_TOIL",
    }
    assert template.metadata["servicenow_capabilities"] == [
        "ITSM",
        "Security Operations",
    ]
    assert template.metadata["version"] == "1.0.0"


def test_security_template_is_served_by_shared_registry_api(client):
    listed = client.get("/api/stack-builder/templates", headers=_auth())
    assert listed.status_code == 200
    ids = {item["template_id"] for item in listed.json()}
    assert {SECURITY_TEMPLATE_ID, CLOUD_TEMPLATE_ID}.issubset(ids)

    response = client.get(
        f"/api/stack-builder/templates/{SECURITY_TEMPLATE_ID}", headers=_auth()
    )
    assert response.status_code == 200
    assert response.json()["pack_id"] == "security_ops"


def test_security_defaults_are_editable():
    resolved = resolve_launch_config(
        SECURITY_TEMPLATE_ID,
        focus_id="cross_system_handoffs",
        selected_system_ids=["servicenow", "runbook_library"],
        weightings={"servicenow": {"role": "workflow_system"}},
    )
    assert resolved["effective"]["pack_ids"] == ["security_ops"]
    assert resolved["effective"]["focus_id"] == "cross_system_handoffs"
    assert resolved["effective"]["selected_system_ids"] == [
        "servicenow",
        "runbook_library",
    ]
    assert resolved["effective"]["roles"]["servicenow"] == "workflow_system"
    assert set(resolved["effective"]["roles"]) == {
        "servicenow",
        "runbook_library",
    }
    assert set(resolved["provenance"]["edited_fields"]) == {
        "focus_id",
        "selected_system_ids",
        "roles",
    }


def test_cloud_and_security_templates_compose_without_replacement():
    resolved = resolve_launch_config(
        CLOUD_TEMPLATE_ID,
        template_ids=[CLOUD_TEMPLATE_ID, SECURITY_TEMPLATE_ID],
    )
    effective = resolved["effective"]
    provenance = resolved["provenance"]
    assert effective["template_ids"] == [CLOUD_TEMPLATE_ID, SECURITY_TEMPLATE_ID]
    assert effective["pack_ids"] == ["cloud_ops", "security_ops"]
    assert effective["selected_system_ids"] == [
        "servicenow",
        "aws_event_source",
        "azure_event_source",
        "runbook_library",
    ]
    assert provenance["untouched"] is True
    boundaries = {item["pack_id"]: item for item in provenance["pack_boundaries"]}
    assert set(boundaries) == {"cloud_ops", "security_ops"}
    assert "RECURRING_RESOLUTION_LOOP" in boundaries["cloud_ops"]["detector_emphasis"]
    assert "SECOPS_REMEDIATION_RECURRENCE" in boundaries["security_ops"]["detector_emphasis"]
    assert boundaries["cloud_ops"]["terminology"]["notification"] == "alert"
    assert boundaries["security_ops"]["terminology"]["ticket"] == "remediation task"


def test_combined_launch_persists_versions_and_effective_configuration(client):
    response = client.post(
        "/api/stack-builder/launch",
        headers=_auth(),
        json={
            "org_id": "b12-combined-org",
            "template_ids": [CLOUD_TEMPLATE_ID, SECURITY_TEMPLATE_ID],
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["templateIds"] == [CLOUD_TEMPLATE_ID, SECURITY_TEMPLATE_ID]
    assert body["packIds"] == ["cloud_ops", "security_ops"]

    run = client.get(f"/api/runs/{body['runId']}", headers=_auth()).json()
    assert run["templateIds"] == [CLOUD_TEMPLATE_ID, SECURITY_TEMPLATE_ID]
    assert run["packVersions"] == {
        "cloud_ops": get_pack_version("cloud_ops"),
        "security_ops": get_pack_version("security_ops"),
    }
    assert run["templateVersions"] == {
        CLOUD_TEMPLATE_ID: "1.0.0",
        SECURITY_TEMPLATE_ID: "1.0.0",
    }
    assert run["effectiveConfiguration"]["pack_ids"] == [
        "cloud_ops",
        "security_ops",
    ]
    assert len(run["packBoundaries"]) == 2


def test_pack_edit_removes_inactive_boundary_but_keeps_template_provenance():
    resolved = resolve_launch_config(
        CLOUD_TEMPLATE_ID,
        template_ids=[CLOUD_TEMPLATE_ID, SECURITY_TEMPLATE_ID],
        pack_ids=["security_ops"],
    )
    assert resolved["effective"]["pack_ids"] == ["security_ops"]
    assert [
        item["pack_id"] for item in resolved["effective"]["pack_boundaries"]
    ] == ["security_ops"]
    assert resolved["provenance"]["template_ids"] == [
        CLOUD_TEMPLATE_ID,
        SECURITY_TEMPLATE_ID,
    ]
    assert "pack_id" in resolved["provenance"]["edited_fields"]


def test_cloud_and_security_packs_execute_together_with_distinct_contracts():
    payload = run(
        mode="offline",
        run_id="b12-cloud-security-runner",
        pack_ids=["cloud_ops", "security_ops"],
    )
    assert payload["packIds"] == ["cloud_ops", "security_ops"]
    packs = {item["packId"]: item for item in payload["packs"]}
    assert set(packs) == {"cloud_ops", "security_ops"}
    assert packs["cloud_ops"]["packVersion"] == get_pack_version("cloud_ops")
    assert packs["security_ops"]["packVersion"] == get_pack_version("security_ops")
    assert "RECURRING_RESOLUTION_LOOP" in packs["cloud_ops"]["detectorsExecuted"]
    assert (
        "OPS_RUNBOOK_DOCUMENTATION_GAP"
        in packs["cloud_ops"]["detectorsExecuted"]
    )
    assert "SECOPS_REMEDIATION_RECURRENCE" in packs["security_ops"]["detectorsExecuted"]
    for opportunity in payload["opportunities"]:
        assert opportunity["packId"] in {"cloud_ops", "security_ops"}
        assert opportunity["packVersion"] == get_pack_version(opportunity["packId"])
        assert all(
            evidence.get("packId") == opportunity["packId"]
            for evidence in opportunity.get("evidence", [])
        )


def test_compute_route_forwards_plural_pack_selection(client, monkeypatch):
    launch = client.post(
        "/api/stack-builder/launch",
        headers=_auth(),
        json={
            "org_id": "b12-compute-org",
            "template_ids": [CLOUD_TEMPLATE_ID, SECURITY_TEMPLATE_ID],
        },
    ).json()
    captured: Dict[str, Any] = {}

    def fake_worker(
        run_id: str,
        mode: str,
        systems: List[str],
        pack: str | None,
        pack_ids: List[str] | None,
    ) -> None:
        captured.update(
            run_id=run_id,
            mode=mode,
            systems=systems,
            pack=pack,
            pack_ids=pack_ids,
        )

    monkeypatch.setattr("app.routes_sprint4_t1._run_trackb_and_persist", fake_worker)
    response = client.post(
        f"/api/runs/{launch['runId']}/compute",
        headers=_auth(),
        json={
            "mode": "offline",
            "systems": ["servicenow"],
            "pack_ids": ["cloud_ops", "security_ops"],
        },
    )
    assert response.status_code == 200
    assert captured["pack"] == "cloud_ops"
    assert captured["pack_ids"] == ["cloud_ops", "security_ops"]


def test_hosted_ai_withholds_only_security_pack(monkeypatch):
    calls: List[Dict[str, Any]] = []

    def fake_enrichment(**kwargs):
        calls.append(kwargs)
        return {
            "perOpportunity": {kwargs["opps"][0]["id"]: {"aiSummary": "ok"}},
            "executiveSummary": "Cloud summary",
            "opportunitiesEnriched": 1,
            "opportunitiesFailed": 0,
            "elapsedSeconds": 0.1,
            "generatedAt": "2026-07-22T10:00:00+00:00",
            "llmModel": "contract-model",
        }

    monkeypatch.setattr(
        "app.pack_aware_enrichment.ai_narrative_blocked_for_pack",
        lambda pack_id: pack_id == "security_ops",
    )
    result = run_pack_aware_enrichment(
        run_id="b12-ai",
        opportunities=[
            {"id": "cloud", "packId": "cloud_ops", "evidenceIds": ["ec", "es"]},
            {"id": "security", "packId": "security_ops", "evidenceIds": ["es"]},
        ],
        evidence=[
            {"id": "ec", "packId": "cloud_ops", "snippet": "cloud"},
            {"id": "es", "packId": "security_ops", "snippet": "secret"},
        ],
        sources_analyzed={},
        pack_ids=["cloud_ops", "security_ops"],
        org_id="b12-org",
        enrichment_fn=fake_enrichment,
    )
    assert len(calls) == 1
    assert calls[0]["pack_id"] == "cloud_ops"
    assert [item["id"] for item in calls[0]["evidence"]] == ["ec"]
    assert result["withheldPackIds"] == ["security_ops"]
    assert result["perOpportunity"]["cloud"]["packId"] == "cloud_ops"
    assert result["perOpportunity"]["security"]["packId"] == "security_ops"
    assert result["perOpportunity"]["security"]["aiNarrativeAvailable"] is False
    assert "unavailable" in result["perOpportunity"]["security"]["aiSummary"]
    assert result["generatedAt"] == "2026-07-22T10:00:00+00:00"
    assert result["llmModel"] == "contract-model"


def test_hosted_security_finding_detail_stays_available(client, monkeypatch):
    launch = client.post(
        "/api/stack-builder/launch",
        headers=_auth(),
        json={"org_id": "b12-hosted-org", "template_ids": [SECURITY_TEMPLATE_ID]},
    ).json()
    run_id = launch["runId"]
    opportunity = {
        "id": "security-detail",
        "title": "Repeated remediation work",
        "aiRationale": "Deterministic Security Operations finding.",
        "packId": "security_ops",
        "evidenceIds": [],
        "_debug": {"detector_id": "SECOPS_REMEDIATION_RECURRENCE"},
    }
    monkeypatch.setattr(
        "app.pack_aware_enrichment.ai_narrative_blocked_for_pack",
        lambda pack_id: pack_id == "security_ops",
    )
    enrichment = run_pack_aware_enrichment(
        run_id=run_id,
        opportunities=[opportunity],
        evidence=[],
        sources_analyzed={},
        pack_ids=["security_ops"],
        org_id="b12-hosted-org",
    )
    db.run_kv_set("opps", run_id, [opportunity])
    db.run_kv_set("llm_enrichment", run_id, enrichment)

    response = client.get(
        f"/api/runs/{run_id}/opportunities/security-detail/enrichment",
        headers=_auth(),
    )
    assert response.status_code == 200, response.text
    detail = response.json()
    assert detail["llmGenerated"] is False
    assert detail["aiNarrativeAvailable"] is False
    assert "unavailable" in detail["aiModeLabel"]
    assert "unavailable" in detail["aiSummary"]


def test_combined_terminology_is_applied_per_pack():
    run_id = "b12-terminology-contract"
    resolved = resolve_launch_config(
        CLOUD_TEMPLATE_ID,
        template_ids=[CLOUD_TEMPLATE_ID, SECURITY_TEMPLATE_ID],
    )
    db.run_set(
        run_id,
        {
            "id": run_id,
            "templateId": CLOUD_TEMPLATE_ID,
            "templateIds": [CLOUD_TEMPLATE_ID, SECURITY_TEMPLATE_ID],
            "templateProvenance": resolved["provenance"],
        },
    )
    rendered = apply_run_terminology(
        [
            {"packId": "cloud_ops", "title": "Notification documentation"},
            {"packId": "security_ops", "title": "Ticket documentation"},
        ],
        run_id,
    )
    assert rendered[0]["title"] == "Alert runbook"
    assert rendered[1]["title"] == "Remediation task playbook"
