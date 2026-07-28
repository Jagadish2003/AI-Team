"""R191-P1 (AT-707) — multi-pack run configuration + multi-pack run-health panel.

Multi-pack runs are composed from:
  * explicit ``pack_ids`` on the launch request (the frontend sends the union of
    the Salesforce-product-declaration packs + the Discovery Plan analysis packs), and
  * multiple selected single-pack templates (``template_ids``), each contributing
    its pack.

The generic template model is unchanged — a template declares exactly ONE pack —
so this file no longer exercises a "template declaring two packs". Instead it
covers the real composition paths and the run-health panel showing one row PER
pack. Runs against the disposable PostgreSQL test DB (conftest `alembic upgrade head`).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app
from app.rbac import seed_owner
from discovery.packs.template_registry import (
    FocusDefaults,
    TemplateDefinition,
    register_template,
    resolve_launch_config,
    unregister_template,
)

_DEV_TOKEN = "dev-token-change-me"
_TWO_PACKS = ["service_cloud", "enterprise_ops"]


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def _auth(org_id: str) -> dict:
    return {"Authorization": f"Bearer {_DEV_TOKEN}", "X-Org-Id": org_id}


def _now_iso(offset_seconds: int = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)).isoformat()


def _owner_org(prefix: str) -> str:
    org_id = f"{prefix}_{uuid4().hex[:8]}"
    seed_owner(org_id, _DEV_TOKEN)
    return org_id


@pytest.fixture
def two_templates():
    """Two registered SINGLE-pack fixture templates (one per pack)."""
    ids = []
    for pack_id, focus in ((_TWO_PACKS[0], "member_customer_service"),
                           (_TWO_PACKS[1], "core_operations")):
        template_id = f"tpl_{pack_id}_{uuid4().hex[:6]}"
        register_template(
            TemplateDefinition(
                template_id=template_id,
                label=f"{pack_id} (test)",
                description="Single-pack template fixture.",
                suggested_systems=["servicenow", "jira"],
                suggested_roles={"servicenow": "workflow_system", "jira": "workflow_system"},
                focus_defaults=FocusDefaults(focus_id=focus, emphasis=[]),
                pack_id=pack_id,
            )
        )
        ids.append(template_id)
    yield ids
    for template_id in ids:
        unregister_template(template_id)


# ── resolve_launch_config: multi-pack composition ─────────────────────────────

def test_explicit_pack_ids_resolve_to_all_packs():
    resolved = resolve_launch_config(None, pack_ids=list(_TWO_PACKS))
    assert resolved["effective"]["pack_ids"] == _TWO_PACKS
    assert resolved["effective"]["pack_id"] == _TWO_PACKS[0]


def test_two_templates_resolve_to_the_union_of_their_packs(two_templates):
    resolved = resolve_launch_config(None, template_ids=two_templates)
    assert resolved["effective"]["pack_ids"] == _TWO_PACKS
    assert resolved["effective"]["pack_id"] == _TWO_PACKS[0]


def test_explicit_pack_ids_override_template_packs(two_templates):
    resolved = resolve_launch_config(None, template_ids=two_templates, pack_ids=["ncino"])
    assert resolved["effective"]["pack_ids"] == ["ncino"]
    assert resolved["effective"]["pack_id"] == "ncino"


# ── Launch endpoint: a multi-pack launch activates every pack on run creation ──

def test_launch_with_pack_ids_activates_both_packs(client):
    org = _owner_org("t5_launch_packids")
    body = {
        "org_id": org,
        "selected_system_ids": ["servicenow", "jira"],
        "pack_ids": _TWO_PACKS,
    }
    resp = client.post("/api/stack-builder/launch", headers=_auth(org), json=body)
    assert resp.status_code == 200
    data = resp.json()
    assert data["packId"] == _TWO_PACKS[0]      # primary (backward-compatible)
    assert data["packIds"] == _TWO_PACKS        # both activate

    run = client.get(f"/api/runs/{data['runId']}", headers=_auth(org)).json()
    assert run["packId"] == _TWO_PACKS[0]
    assert run["packIds"] == _TWO_PACKS


def test_launch_with_two_templates_activates_both_packs(client, two_templates):
    org = _owner_org("t5_launch_templates")
    body = {
        "org_id": org,
        "template_ids": two_templates,
        "selected_system_ids": ["servicenow", "jira"],
    }
    resp = client.post("/api/stack-builder/launch", headers=_auth(org), json=body)
    assert resp.status_code == 200
    assert resp.json()["packIds"] == _TWO_PACKS


# ── Run-health packs panel: one row per pack for a multi-pack run ──────────────

def _seed_multi_pack_run(org_id: str) -> str:
    run_id = f"run_{uuid4().hex[:10]}"
    db.upsert_run(
        run_id,
        {
            "id": run_id,
            "org_id": org_id,
            "orgId": org_id,
            "status": "complete",
            "startedAt": _now_iso(-120),
            "updatedAt": _now_iso(),
            "packId": "service_cloud",
            "packName": "Service Cloud",
            "packVersion": "1.0.0",
            "executedDetectorIds": ["REPETITION", "HANDOFF_FRICTION"],
            "packExecutedAt": _now_iso(-30),
            "packIds": _TWO_PACKS,
            "packVersions": {"service_cloud": "1.0.0", "enterprise_ops": "1.0.0"},
            "packs": [
                {
                    "packId": "service_cloud",
                    "packName": "Service Cloud",
                    "packVersion": "1.0.0",
                    "detectorsExecuted": ["REPETITION", "HANDOFF_FRICTION"],
                    "packExecutedAt": _now_iso(-30),
                },
                {
                    "packId": "enterprise_ops",
                    "packName": "Enterprise Operations Intelligence",
                    "packVersion": "1.0.0",
                    "detectorsExecuted": ["ENT_INCIDENT_RESOLUTION_LAG"],
                    "packExecutedAt": _now_iso(-30),
                },
            ],
            "selectedSystemIds": ["servicenow", "jira"],
            "systemCount": 2,
            "source": "stack_builder",
        },
    )
    db.run_kv_set(
        "status", run_id,
        {"runId": run_id, "status": "complete",
         "counts": {"opportunities": 1, "evidence": 0}, "errors": {},
         "updatedAt": _now_iso()},
    )
    return run_id


def test_run_health_packs_panel_shows_one_row_per_pack(client):
    org = _owner_org("t5_health")
    run_id = _seed_multi_pack_run(org)

    body = client.get("/api/run-health/packs", headers=_auth(org)).json()
    assert body["run_id"] == run_id
    packs = body["packs"]
    assert len(packs) == 2  # more than one row

    by_id = {p["pack_id"]: p for p in packs}
    assert set(by_id) == set(_TWO_PACKS)
    assert by_id["service_cloud"]["detectors"] == ["REPETITION", "HANDOFF_FRICTION"]
    assert by_id["enterprise_ops"]["detectors"] == ["ENT_INCIDENT_RESOLUTION_LAG"]
    assert by_id["enterprise_ops"]["pack_version"] == "1.0.0"
    assert by_id["enterprise_ops"]["detector_count"] == 1
