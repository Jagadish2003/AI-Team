"""2.0-C1 T1 (AT-826) — pack compatibility refusal over the HTTP activation edges.

Parent-story criterion: **AC1** — a pack declaring an unmet platform range cannot
be activated; the refusal names the unmet requirement.

Both activation edges are exercised end to end:

  * ``POST /api/stack-builder/launch``   — the primary edge (Stack Builder)
  * ``POST /api/runs/{run_id}/compute``  — the second edge (direct compute/replay)

and the two properties that make the refusal trustworthy:

  * a refused launch creates NO run — an incompatible pack never leaves a
    half-created run record behind;
  * a refused compute never queues the background task and never flips the run to
    "running".

The compatibility RULE is pinned in ``discovery/tests/test_pack_compatibility.py``
and the shared app-layer gate in ``tests/unit/test_pack_activation_gate.py``; this
suite pins the HTTP contract (409 + a reason naming the requirement) and the
run-record persistence of the launch-time verdict.
"""
from __future__ import annotations

import os
from typing import Any, Dict

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app
from discovery.packs.pack_config import PACK_REGISTRY
from discovery.packs.platform_capabilities import PLATFORM_VERSION

_FUTURE_PACK_ID = "test_contract_future_pack"
_MISSING_CONCEPT_PACK_ID = "test_contract_missing_concept_pack"


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def _auth() -> Dict[str, str]:
    return {"Authorization": f"Bearer {os.getenv('DEV_JWT', 'dev-token-change-me')}"}


def _test_pack(pack_id: str, compatibility: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "packId": pack_id,
        "packVersion": "7.7.7",
        "packName": f"Contract test pack {pack_id}",
        "domain": "service_cloud",
        "pack_domain": "service_cloud",
        "detectors": [],
        "ui_labels_path": None,
        "llm_context": "test",
        "compatibility": compatibility,
    }


@pytest.fixture
def incompatible_packs(monkeypatch):
    """Register packs this platform version cannot satisfy.

    Registered (not merely referenced) because ``get_pack()`` resolves an unknown
    id to the default pack — an unregistered id would be checked as
    ``service_cloud`` and correctly pass.
    """
    packs = {
        _FUTURE_PACK_ID: _test_pack(
            _FUTURE_PACK_ID,
            {
                "minPlatformVersion": "99.0.0",
                "maxPlatformVersion": None,
                "requiredConcepts": ["case_workflow"],
            },
        ),
        _MISSING_CONCEPT_PACK_ID: _test_pack(
            _MISSING_CONCEPT_PACK_ID,
            {
                "minPlatformVersion": "1.0.0",
                "maxPlatformVersion": None,
                "requiredConcepts": ["case_workflow", "astral_projection_workflow"],
            },
        ),
    }
    for pack_id, pack in packs.items():
        monkeypatch.setitem(PACK_REGISTRY, pack_id, pack)
    return packs


def _launch_body(**overrides: Any) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "org_id": "default",
        "industry_id": "financial_services",
        "selected_system_ids": ["salesforce", "servicenow"],
        "pack_id": "service_cloud",
        "weightings": {},
    }
    body.update(overrides)
    return body


# ── AC1 — the launch edge refuses an incompatible pack ────────────────────────


class TestLaunchRefusesIncompatiblePack:
    def test_unmet_platform_range_is_refused_with_409(
        self, client, incompatible_packs
    ):
        response = client.post(
            "/api/stack-builder/launch",
            json=_launch_body(pack_id=_FUTURE_PACK_ID),
            headers=_auth(),
        )
        assert response.status_code == 409, response.text

    def test_refusal_detail_names_the_unmet_requirement(
        self, client, incompatible_packs
    ):
        response = client.post(
            "/api/stack-builder/launch",
            json=_launch_body(pack_id=_FUTURE_PACK_ID),
            headers=_auth(),
        )
        detail = response.json()["detail"]
        # AC1: the pack, the declared bound, and the actual platform version.
        assert _FUTURE_PACK_ID in detail
        assert "99.0.0" in detail
        assert PLATFORM_VERSION in detail

    def test_refusal_detail_names_an_unmet_normalised_concept(
        self, client, incompatible_packs
    ):
        response = client.post(
            "/api/stack-builder/launch",
            json=_launch_body(pack_id=_MISSING_CONCEPT_PACK_ID),
            headers=_auth(),
        )
        assert response.status_code == 409
        assert "astral_projection_workflow" in response.json()["detail"]

    def test_multi_pack_selection_is_refused_when_one_pack_is_incompatible(
        self, client, incompatible_packs
    ):
        response = client.post(
            "/api/stack-builder/launch",
            json=_launch_body(pack_ids=["service_cloud", _FUTURE_PACK_ID]),
            headers=_auth(),
        )
        assert response.status_code == 409
        detail = response.json()["detail"]
        assert _FUTURE_PACK_ID in detail
        # The compatible pack in the selection must not be reported as refused.
        assert "'service_cloud'" not in detail

    def test_a_refused_launch_creates_no_run(
        self, client, incompatible_packs, monkeypatch
    ):
        # The gate runs before the run id is generated and before any persistence,
        # so an incompatible pack never leaves a half-created run behind.
        import app.routes_stack_builder_launch as launch_module

        upserts: list = []
        monkeypatch.setattr(
            launch_module,
            "upsert_run",
            lambda run_id, record: upserts.append(run_id),
        )
        response = client.post(
            "/api/stack-builder/launch",
            json=_launch_body(pack_id=_FUTURE_PACK_ID),
            headers=_auth(),
        )
        assert response.status_code == 409
        assert upserts == []


# ── Compatible launches are unaffected, and record the verdict ────────────────


class TestLaunchAcceptsCompatiblePack:
    def test_shipped_pack_still_launches(self, client):
        response = client.post(
            "/api/stack-builder/launch", json=_launch_body(), headers=_auth()
        )
        assert response.status_code == 200, response.text
        assert response.json()["packId"] == "service_cloud"

    def test_launch_records_the_compatibility_verdict_on_the_run(self, client):
        response = client.post(
            "/api/stack-builder/launch",
            json=_launch_body(pack_ids=["service_cloud", "cloud_ops"]),
            headers=_auth(),
        )
        assert response.status_code == 200, response.text
        run_id = response.json()["runId"]

        run = db.run_get(run_id)
        assert run["platformVersion"] == PLATFORM_VERSION
        compatibility = run["packCompatibility"]
        assert sorted(compatibility) == ["cloud_ops", "service_cloud"]
        assert compatibility["cloud_ops"]["compatible"] is True
        assert compatibility["cloud_ops"]["minPlatformVersion"] == "1.9.0"
        assert (
            "resolution_signature"
            in compatibility["cloud_ops"]["requiredConcepts"]
        )

    def test_launch_persists_the_verdict_as_run_scoped_kv(self, client):
        response = client.post(
            "/api/stack-builder/launch",
            json=_launch_body(pack_id="security_ops"),
            headers=_auth(),
        )
        assert response.status_code == 200, response.text
        run_id = response.json()["runId"]

        stored = db.run_kv_get("pack_compatibility", run_id, {})
        assert stored["security_ops"]["compatible"] is True
        assert stored["security_ops"]["platformVersion"] == PLATFORM_VERSION


# ── AC1 — the compute edge refuses an incompatible pack ───────────────────────


class TestComputeRefusesIncompatiblePack:
    def _create_run(self, client, **overrides: Any) -> str:
        response = client.post(
            "/api/stack-builder/launch",
            json=_launch_body(**overrides),
            headers=_auth(),
        )
        assert response.status_code == 200, response.text
        return response.json()["runId"]

    @pytest.fixture(autouse=True)
    def no_real_discovery(self, monkeypatch):
        """Stub the background materialization.

        A compute request that PASSES the gate would otherwise run a full offline
        discovery inside the test client (TestClient drains background tasks
        synchronously). The gate is what this suite tests; executing discovery is
        the multi-pack execution suite's job.
        """
        import app.routes_sprint4_t1 as compute_module

        self.background_calls: list = []
        monkeypatch.setattr(
            compute_module,
            "_run_trackb_and_persist",
            lambda *args, **kwargs: self.background_calls.append(args),
        )

    def test_incompatible_pack_in_the_request_is_refused_with_409(
        self, client, incompatible_packs
    ):
        run_id = self._create_run(client)
        response = client.post(
            f"/api/runs/{run_id}/compute",
            json={
                "mode": "offline",
                "systems": ["salesforce"],
                "pack_ids": [_FUTURE_PACK_ID],
            },
            headers=_auth(),
        )
        assert response.status_code == 409, response.text
        detail = response.json()["detail"]
        assert _FUTURE_PACK_ID in detail
        assert "99.0.0" in detail

    def test_refused_compute_does_not_flip_the_run_to_running(
        self, client, incompatible_packs
    ):
        run_id = self._create_run(client)
        response = client.post(
            f"/api/runs/{run_id}/compute",
            json={
                "mode": "offline",
                "systems": ["salesforce"],
                "pack": _FUTURE_PACK_ID,
            },
            headers=_auth(),
        )
        assert response.status_code == 409
        # The gate runs BEFORE _set_status("running") and before the background
        # task is queued, so the run is left exactly as the launch left it.
        assert db.run_get(run_id)["status"] == "created"
        assert db.run_kv_get("status", run_id, {}).get("status") != "running"
        assert self.background_calls == []

    def test_unknown_run_still_404s_before_the_compatibility_gate(
        self, client, incompatible_packs
    ):
        # Ordering guard: a nonexistent run is a 404, not a 409 — the gate must not
        # pre-empt the existence check.
        response = client.post(
            "/api/runs/no-such-run/compute",
            json={"mode": "offline", "systems": [], "pack": _FUTURE_PACK_ID},
            headers=_auth(),
        )
        assert response.status_code == 404

    def test_compatible_compute_request_is_accepted(self, client):
        run_id = self._create_run(client)
        response = client.post(
            f"/api/runs/{run_id}/compute",
            json={
                "mode": "offline",
                "systems": ["salesforce"],
                "pack": "service_cloud",
            },
            headers=_auth(),
        )
        assert response.status_code == 200, response.text
        assert response.json()["ok"] is True
        # The gate let it through — materialization was queued.
        assert len(self.background_calls) == 1
