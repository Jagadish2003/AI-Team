"""R191-P1 T5 (AT-707) — multi-pack run configuration + multi-pack run-health panel.

Covers AC5: a template declaring two packs activates BOTH on run creation, and the
run-health pack panel shows both executions (more than one row).

Two halves:
  * Template model → launch: a template's `packs` list is honored end to end.
    `resolve_launch_config` returns every declared pack; the launch endpoint
    persists them as the run's `packIds`, so an untouched multi-pack template
    activates all of its packs. An explicit caller selection still overrides.
  * Run-health `packs_view`: a run whose record carries a per-pack execution list
    (`run["packs"]`, persisted by materialize for a multi-pack run) is reported as
    one pack row PER pack — not collapsed to one.

Multi-pack behavior is demonstrated with a REGISTERED FIXTURE template built from
two real, shipped packs (`service_cloud` + `enterprise_ops`) via the documented
`register_template` hook, so no production template/registry is altered. Runs
against the disposable PostgreSQL test DB (conftest `alembic upgrade head`).
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
def combined_template():
    """A registered fixture template declaring TWO shipped packs."""
    template_id = f"combined_ops_{uuid4().hex[:6]}"
    register_template(
        TemplateDefinition(
            template_id=template_id,
            label="Combined operations (test)",
            description="Two-pack template fixture for R191-P1 T5.",
            suggested_systems=["servicenow", "jira"],
            suggested_roles={"servicenow": "workflow_system", "jira": "workflow_system"},
            focus_defaults=FocusDefaults(focus_id="core_operations", emphasis=[]),
            pack_id="service_cloud",
            packs=list(_TWO_PACKS),
        )
    )
    yield template_id
    unregister_template(template_id)


# ── Template model: packs list normalization ──────────────────────────────────

def test_template_definition_normalizes_packs_and_primary():
    defn = TemplateDefinition(
        template_id="t_norm",
        label="x", description="x",
        suggested_systems=[], suggested_roles={},
        focus_defaults=FocusDefaults(focus_id="core_operations"),
        pack_id="service_cloud",
        packs=["service_cloud", "enterprise_ops", "service_cloud"],  # dup collapses
    )
    assert defn.packs == ["service_cloud", "enterprise_ops"]
    assert defn.pack_id == "service_cloud"  # primary = first


def test_single_pack_template_defaults_packs_to_pack_id():
    defn = TemplateDefinition(
        template_id="t_single",
        label="x", description="x",
        suggested_systems=[], suggested_roles={},
        focus_defaults=FocusDefaults(focus_id="core_operations"),
        pack_id="ncino",
    )
    assert defn.packs == ["ncino"]


# ── resolve_launch_config: template packs honored end to end ───────────────────

def test_untouched_multi_pack_template_resolves_all_packs(combined_template):
    resolved = resolve_launch_config(combined_template)  # no caller pack selection
    assert resolved["effective"]["pack_ids"] == _TWO_PACKS
    assert resolved["effective"]["pack_id"] == "service_cloud"
    # Untouched => no pack edit recorded.
    assert "pack_id" not in resolved["provenance"]["edited_fields"]


def test_explicit_pack_selection_overrides_template_packs(combined_template):
    resolved = resolve_launch_config(combined_template, pack_ids=["ncino"])
    assert resolved["effective"]["pack_ids"] == ["ncino"]
    assert resolved["effective"]["pack_id"] == "ncino"


# ── Launch endpoint: a two-pack template activates both on run creation ────────

def test_launch_from_multi_pack_template_activates_both_packs(client, combined_template):
    org = _owner_org("t5_launch")
    body = {"org_id": org, "template_id": combined_template}  # untouched launch
    resp = client.post("/api/stack-builder/launch", headers=_auth(org), json=body)
    assert resp.status_code == 200
    data = resp.json()
    assert data["packId"] == "service_cloud"        # primary (backward-compatible)
    assert data["packIds"] == _TWO_PACKS            # both activate

    run = client.get(f"/api/runs/{data['runId']}", headers=_auth(org)).json()
    assert run["packId"] == "service_cloud"
    assert run["packIds"] == _TWO_PACKS


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
            # Primary scalar fields (backward-compatible).
            "packId": "service_cloud",
            "packName": "Service Cloud",
            "packVersion": "1.0.0",
            "executedDetectorIds": ["REPETITION", "HANDOFF_FRICTION"],
            "packExecutedAt": _now_iso(-30),
            # R191-P1 T2/T3 multi-pack surface persisted on the run record.
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


def test_run_health_packs_panel_shows_one_row_per_pack(client, combined_template):
    org = _owner_org("t5_health")
    run_id = _seed_multi_pack_run(org)

    body = client.get("/api/run-health/packs", headers=_auth(org)).json()
    assert body["run_id"] == run_id
    packs = body["packs"]
    assert len(packs) == 2  # AC5: more than one row

    by_id = {p["pack_id"]: p for p in packs}
    assert set(by_id) == set(_TWO_PACKS)
    assert by_id["service_cloud"]["detectors"] == ["REPETITION", "HANDOFF_FRICTION"]
    assert by_id["enterprise_ops"]["detectors"] == ["ENT_INCIDENT_RESOLUTION_LAG"]
    assert by_id["enterprise_ops"]["pack_version"] == "1.0.0"
    assert by_id["enterprise_ops"]["detector_count"] == 1
