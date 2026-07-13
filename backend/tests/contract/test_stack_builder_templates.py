"""
test_stack_builder_templates.py — R18-C1 T1

Contract + unit tests for the Stack Builder Template Definition Model.

T1 delivers a GENERIC, config-driven template model plus listing support so the
frontend can ask the backend "what templates are available?" instead of owning a
hardcoded TEMPLATES array.

Covers:
  GET /api/stack-builder/templates              — lists templates from config
  GET /api/stack-builder/templates/{id}         — one template's defaults
  GET /api/stack-builder/templates/unknown      — 404
  AC4/AC8 — genericness: a second template added by CONFIG ONLY appears through
            the same endpoint with no route/model change.
  Unit — registry accessors + pack validation.
"""
from __future__ import annotations

import os
from typing import Dict

import pytest
from fastapi.testclient import TestClient

from app.main import app
from discovery.packs.template_registry import (
    FocusDefaults,
    TemplateDefinition,
    get_template,
    list_templates,
    register_template,
    resolve_launch_config,
    unregister_template,
)
from discovery.packs.pack_config import list_packs
from discovery.lending_scorer import is_lending_detector


# ── Test client ───────────────────────────────────────────────────────────────

@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def _auth() -> Dict[str, str]:
    return {"Authorization": f"Bearer {os.getenv('DEV_JWT', 'dev-token-change-me')}"}


_PRODUCTION_TEMPLATE_IDS = {
    "commercial_lending",
    "service_operations",
    "revenue_operations",
}


# ── Endpoint: list ────────────────────────────────────────────────────────────

def test_list_templates_returns_200(client):
    resp = client.get("/api/stack-builder/templates", headers=_auth())
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_list_templates_includes_production_templates(client):
    resp = client.get("/api/stack-builder/templates", headers=_auth())
    ids = {t["template_id"] for t in resp.json()}
    assert _PRODUCTION_TEMPLATE_IDS.issubset(ids)


def test_list_templates_item_shape(client):
    resp = client.get("/api/stack-builder/templates", headers=_auth())
    item = next(t for t in resp.json() if t["template_id"] == "commercial_lending")
    # Every field the frontend needs to render + pre-populate an editable setup.
    for key in (
        "template_id",
        "label",
        "description",
        "suggested_systems",
        "suggested_roles",
        "focus_defaults",
        "pack_id",
        "terminology",
        "metadata",
    ):
        assert key in item, f"missing field: {key}"
    assert item["focus_defaults"]["focus_id"] == "approvals_compliance"
    assert "emphasis" in item["focus_defaults"]


def test_commercial_lending_template_defaults(client):
    """The lending template bundles the real lending pack + lending language."""
    resp = client.get("/api/stack-builder/templates", headers=_auth())
    lending = next(
        t for t in resp.json() if t["template_id"] == "commercial_lending"
    )
    assert lending["pack_id"] == "ncino"  # the real nCino Lending pack
    assert "salesforce_ncino" in lending["suggested_systems"]
    assert lending["suggested_roles"]["salesforce_ncino"] == "system_of_record"
    # Lending terminology surfaces borrowers / facilities / covenants (T4 consumes it).
    assert set(lending["terminology"].values()) >= {"borrower", "facility", "covenant"}


# ── Endpoint: single template ─────────────────────────────────────────────────

def test_get_single_template(client):
    resp = client.get(
        "/api/stack-builder/templates/commercial_lending", headers=_auth()
    )
    assert resp.status_code == 200
    assert resp.json()["template_id"] == "commercial_lending"


def test_get_unknown_template_returns_404(client):
    resp = client.get(
        "/api/stack-builder/templates/does_not_exist", headers=_auth()
    )
    assert resp.status_code == 404


# ── AC4 / AC8 — genericness: a second template by CONFIG ONLY ──────────────────

@pytest.fixture
def fixture_template():
    """Register a test-fixture template via config only, then clean up."""
    defn = TemplateDefinition(
        template_id="insurance_fixture",
        label="Insurance (test fixture)",
        description="Proves a second template needs configuration only.",
        suggested_systems=["salesforce_sc", "servicenow"],
        suggested_roles={
            "salesforce_sc": "system_of_record",
            "servicenow": "workflow_system",
        },
        focus_defaults=FocusDefaults(
            focus_id="core_operations", emphasis=["intake_requests"]
        ),
        pack_id="service_cloud",
        terminology={"customer": "policyholder", "obligation": "policy term"},
        metadata={"industry_id": "insurance", "source": "test_fixture"},
    )
    register_template(defn)
    try:
        yield defn
    finally:
        unregister_template(defn.template_id)


def test_second_template_appears_by_config_only(client, fixture_template):
    """AC4/AC8: adding a template requires NO route or model change."""
    resp = client.get("/api/stack-builder/templates", headers=_auth())
    assert resp.status_code == 200
    ids = {t["template_id"] for t in resp.json()}
    assert "insurance_fixture" in ids, (
        "A config-only template must appear through the existing endpoint with "
        "no code change."
    )

    # And it is fully resolvable through the same by-id endpoint.
    one = client.get(
        "/api/stack-builder/templates/insurance_fixture", headers=_auth()
    )
    assert one.status_code == 200
    body = one.json()
    assert body["label"] == "Insurance (test fixture)"
    assert body["pack_id"] == "service_cloud"
    assert body["terminology"]["customer"] == "policyholder"


def test_fixture_template_removed_after_teardown(client):
    """The fixture template must not leak into other tests (clean registry)."""
    resp = client.get("/api/stack-builder/templates", headers=_auth())
    ids = {t["template_id"] for t in resp.json()}
    assert "insurance_fixture" not in ids


# ── Auth ──────────────────────────────────────────────────────────────────────

def test_list_templates_requires_auth(client):
    resp = client.get("/api/stack-builder/templates")
    assert resp.status_code in (401, 403)


# ── Unit — registry accessors ─────────────────────────────────────────────────

def test_get_template_known_and_unknown():
    assert get_template("commercial_lending") is not None
    assert get_template("nope") is None


def test_list_templates_returns_definitions():
    ids = {t.template_id for t in list_templates()}
    assert _PRODUCTION_TEMPLATE_IDS.issubset(ids)


def test_every_template_references_a_real_pack():
    known = set(list_packs())
    for t in list_templates():
        assert t.pack_id in known, (
            f"template {t.template_id} references unknown pack {t.pack_id}"
        )


def test_register_template_rejects_unknown_pack():
    bad = TemplateDefinition(
        template_id="bad_pack",
        label="Bad",
        description="",
        suggested_systems=[],
        suggested_roles={},
        focus_defaults=FocusDefaults(focus_id="core_operations"),
        pack_id="no_such_pack",
    )
    with pytest.raises(ValueError):
        register_template(bad)
    # ensure the failed registration did not leak
    assert get_template("bad_pack") is None


# ── R18-C1 T2 — lending instance + launch resolution (unit) ───────────────────

def test_lending_template_detector_emphasis_are_real_lending_detectors():
    """The lending template emphasises exactly the ncino lending detectors."""
    lending = get_template("commercial_lending")
    assert lending.detector_emphasis, "lending template must declare emphasis"
    for det in lending.detector_emphasis:
        assert is_lending_detector(det), (
            f"{det} is not a known lending detector (lending_scorer._LENDING_SCORES)"
        )


def test_lending_template_endpoint_exposes_detector_emphasis(client):
    resp = client.get(
        "/api/stack-builder/templates/commercial_lending", headers=_auth()
    )
    assert resp.status_code == 200
    assert "COVENANT_TRACKING_GAP" in resp.json()["detector_emphasis"]


def test_resolve_untouched_template_fills_lending_defaults():
    """AC2: an untouched template resolves to the lending pack + focus, no edits."""
    r = resolve_launch_config("commercial_lending")
    assert r["effective"]["pack_id"] == "ncino"
    assert r["effective"]["focus_id"] == "approvals_compliance"
    assert r["effective"]["selected_system_ids"] == [
        "salesforce_ncino",
        "jira",
        "servicenow",
        "confluence",
    ]
    assert r["provenance"]["untouched"] is True
    assert r["provenance"]["edited_fields"] == []


def test_resolve_records_only_the_edited_fields():
    """AC5: a submitted value that diverges from the default is recorded once."""
    r = resolve_launch_config(
        "commercial_lending",
        pack_id="service_cloud",              # edited
        focus_id="approvals_compliance",      # unchanged
        selected_system_ids=["salesforce_ncino", "jira", "servicenow", "confluence"],
        weightings={"jira": {"role": "documentation_system"}},  # role edited
    )
    assert set(r["provenance"]["edited_fields"]) == {"pack_id", "roles"}
    assert r["provenance"]["untouched"] is False
    assert r["effective"]["pack_id"] == "service_cloud"
    assert r["effective"]["roles"]["jira"] == "documentation_system"


def test_resolve_no_template_is_passthrough():
    r = resolve_launch_config(None, pack_id="service_cloud", focus_id="core_operations")
    assert r["provenance"]["applied"] is False
    assert r["effective"]["pack_id"] == "service_cloud"


def test_resolve_unknown_template_is_passthrough():
    r = resolve_launch_config("nope", pack_id="service_cloud")
    assert r["provenance"]["applied"] is False
    assert r["effective"]["pack_id"] == "service_cloud"
