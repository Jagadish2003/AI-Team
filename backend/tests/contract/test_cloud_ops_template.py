"""
Contract tests for MSP-B6 T5 (AT-740) — the Managed Cloud Operations template.

The template is the SECOND production instance of the R18-C1 template model,
proving a new template is configuration only. It is delivered as a single
registry entry (template_registry.TEMPLATE_REGISTRY) served by the SAME
registry-driven Stack Builder route as every other template.

Acceptance criteria:
  AC1 — Selecting "Managed Cloud Operations" pre-populates systems, roles, focus,
        and pack per Section 2; all fields are editable; the selection is recorded
        on the run.
  AC2 — Delivered with zero template-model code changes (the model + routes are
        unchanged; only a config entry is added).
  AC3 — Registry entry created via the same registry-driven path used by other
        templates — no bespoke path introduced.
"""
from __future__ import annotations

import os
from typing import Dict

import pytest
from fastapi.testclient import TestClient

from app.main import app

from discovery.packs.template_registry import (
    get_template,
    list_templates,
    resolve_launch_config,
    template_defaults_snapshot,
)
from discovery.packs.pack_config import list_packs, is_cloud_ops_pack
from discovery.packs.focus_affinity import is_valid_focus, detector_matches_focus
from discovery.packs.cloud_ops_scorer import is_cloud_ops_detector

TEMPLATE_ID = "managed_cloud_operations"


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def _auth() -> Dict[str, str]:
    return {"Authorization": f"Bearer {os.getenv('DEV_JWT', 'dev-token-change-me')}"}


# ── AC1 — pre-populates systems / roles / focus / pack per spec ─────────────────


class TestAC1TemplateDefaults:

    def test_template_registered(self):
        defn = get_template(TEMPLATE_ID)
        assert defn is not None
        assert defn.label == "Managed Cloud Operations"

    def test_pack_is_cloud_ops(self):
        assert get_template(TEMPLATE_ID).pack_id == "cloud_ops"
        assert is_cloud_ops_pack("cloud_ops")

    def test_focus_is_core_operations_and_valid(self):
        defn = get_template(TEMPLATE_ID)
        assert defn.focus_defaults.focus_id == "core_operations"
        assert is_valid_focus("core_operations")

    def test_systems_and_roles_per_section_2(self):
        defn = get_template(TEMPLATE_ID)
        # ServiceNow — system of record.
        assert "servicenow" in defn.suggested_systems
        assert defn.suggested_roles["servicenow"] == "system_of_record"
        # AWS/Azure event sources — operational signal.
        assert defn.suggested_roles["aws_event_source"] == "operational_signal_source"
        assert defn.suggested_roles["azure_event_source"] == "operational_signal_source"
        # Runbook library — supporting (documentation lane).
        assert defn.suggested_roles["runbook_library"] == "documentation_system"

    def test_detector_emphasis_are_real_cloud_ops_detectors(self):
        defn = get_template(TEMPLATE_ID)
        assert defn.detector_emphasis, "template must declare its emphasised detectors"
        for det in defn.detector_emphasis:
            assert is_cloud_ops_detector(det), f"{det} is not a cloud_ops detector"

    def test_focus_emphasises_this_packs_detectors(self):
        # core_operations focus already emphasises this pack's detectors — no code change.
        assert detector_matches_focus("core_operations", "RECURRING_RESOLUTION_LOOP")
        assert detector_matches_focus("core_operations", "ALERT_TRIAGE_TOIL")

    def test_terminology_speaks_noc(self):
        terms = get_template(TEMPLATE_ID).terminology
        assert set(terms.values()) >= {"incident", "alert", "runbook", "MTTR"}

    def test_untouched_launch_resolves_to_cloud_ops_pack_and_focus(self):
        """AC1: an untouched selection pre-populates pack, focus, and systems."""
        r = resolve_launch_config(TEMPLATE_ID)
        assert r["effective"]["pack_id"] == "cloud_ops"
        assert r["effective"]["focus_id"] == "core_operations"
        assert r["effective"]["selected_system_ids"] == [
            "servicenow", "aws_event_source", "azure_event_source", "runbook_library",
        ]
        assert r["effective"]["roles"]["servicenow"] == "system_of_record"
        assert r["provenance"]["applied"] is True
        assert r["provenance"]["untouched"] is True
        assert r["provenance"]["edited_fields"] == []

    def test_fields_are_editable(self):
        """AC1: every default is editable — a submitted value wins and is recorded."""
        r = resolve_launch_config(
            TEMPLATE_ID,
            pack_id="service_cloud",                       # edited pack
            focus_id="cross_system_handoffs",              # edited focus
            selected_system_ids=["servicenow"],            # edited systems
            weightings={"servicenow": {"role": "workflow_system"}},  # edited role
        )
        assert set(r["provenance"]["edited_fields"]) == {
            "pack_id", "focus_id", "selected_system_ids", "roles",
        }
        assert r["provenance"]["untouched"] is False
        assert r["effective"]["pack_id"] == "service_cloud"
        assert r["effective"]["focus_id"] == "cross_system_handoffs"
        assert r["effective"]["roles"]["servicenow"] == "workflow_system"

    def test_defaults_snapshot_shape(self):
        snap = template_defaults_snapshot(get_template(TEMPLATE_ID))
        assert snap["pack_id"] == "cloud_ops"
        assert snap["focus_id"] == "core_operations"
        assert "runbook_library" in snap["suggested_systems"]


# ── AC3 — served by the same registry-driven path (no bespoke route) ────────────


class TestAC3SameRegistryPath:

    def test_listed_by_the_shared_templates_endpoint(self, client):
        resp = client.get("/api/stack-builder/templates", headers=_auth())
        assert resp.status_code == 200
        ids = {t["template_id"] for t in resp.json()}
        assert TEMPLATE_ID in ids

    def test_single_template_endpoint_returns_it(self, client):
        resp = client.get(f"/api/stack-builder/templates/{TEMPLATE_ID}", headers=_auth())
        assert resp.status_code == 200
        body = resp.json()
        assert body["template_id"] == TEMPLATE_ID
        assert body["pack_id"] == "cloud_ops"
        assert body["focus_defaults"]["focus_id"] == "core_operations"
        assert "SHARED_CI_HOTSPOT" in body["detector_emphasis"]

    def test_item_shape_matches_the_other_templates(self, client):
        resp = client.get("/api/stack-builder/templates", headers=_auth())
        item = next(t for t in resp.json() if t["template_id"] == TEMPLATE_ID)
        for key in (
            "template_id", "label", "description", "suggested_systems",
            "suggested_roles", "focus_defaults", "pack_id", "detector_emphasis",
            "terminology", "metadata",
        ):
            assert key in item, f"missing field: {key}"


# ── AC2 — configuration only; the template model stays generic ──────────────────


class TestAC2ConfigOnly:

    def test_appears_in_the_generic_registry_listing(self):
        ids = {t.template_id for t in list_templates()}
        assert TEMPLATE_ID in ids

    def test_references_a_real_registered_pack(self):
        assert get_template(TEMPLATE_ID).pack_id in set(list_packs())

    def test_metadata_records_managed_services_lane_provenance(self):
        meta = get_template(TEMPLATE_ID).metadata
        assert meta.get("lane") == "managed_services"
        assert meta.get("source") == "MSP-B6"
