"""
test_multi_pack_run_config.py — R191-P1 T1 (AT-703)

Multi-Pack Discovery Runs, Task 1: run configuration accepts
``pack_ids: list[str]`` (order-preserving, de-duplicated) in place of the
singular ``pack_id``.

This is groundwork for the parent story's AC2 regression bar — a single-pack
selection must behave EXACTLY as today. These tests pin:

  * the shared normalisation primitive (order-preserving, de-duplicated);
  * the run-config request models (LaunchRequest / ComputeRequest) reconciling
    the singular alias and the list into one selection, primary-first;
  * the launch route persisting the full pack_ids list on the run record + KV
    while keeping the singular packId byte-identical for single-pack launches.

Execution over multiple packs, provenance stamping, and the UI multi-select are
LATER P1 tasks and are intentionally NOT exercised here.
"""
from __future__ import annotations

import os
from typing import Dict

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.routes_stack_builder_launch import LaunchRequest
from app.routes_sprint4_t1 import ComputeRequest
from discovery.packs.pack_config import normalize_pack_ids


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def _auth() -> Dict[str, str]:
    return {"Authorization": f"Bearer {os.getenv('DEV_JWT', 'dev-token-change-me')}"}


# ── normalize_pack_ids — the shared primitive ─────────────────────────────────

class TestNormalizePackIds:
    def test_none_yields_empty_list(self):
        assert normalize_pack_ids(None) == []

    def test_bare_string_is_wrapped(self):
        assert normalize_pack_ids("service_cloud") == ["service_cloud"]

    def test_single_element_is_unchanged(self):
        # The regression bar: a single-pack selection is identical to today.
        assert normalize_pack_ids(["ncino"]) == ["ncino"]

    def test_order_is_preserved(self):
        assert normalize_pack_ids(["ncino", "service_cloud", "strs_benefits"]) == [
            "ncino",
            "service_cloud",
            "strs_benefits",
        ]

    def test_duplicates_collapse_keeping_first_occurrence(self):
        assert normalize_pack_ids(
            ["ncino", "service_cloud", "ncino", "service_cloud"]
        ) == ["ncino", "service_cloud"]

    def test_empty_and_whitespace_and_non_string_dropped(self):
        assert normalize_pack_ids(
            ["", "  ", "ncino", None, 123, "  service_cloud  "]  # type: ignore[list-item]
        ) == ["ncino", "service_cloud"]

    def test_unknown_ids_not_filtered(self):
        # Per-id validation/fallback stays get_pack()'s job, so behaviour is
        # unchanged for a single id — normalisation must not silently drop it.
        assert normalize_pack_ids(["not_a_real_pack"]) == ["not_a_real_pack"]


# ── LaunchRequest — singular/list reconciliation ──────────────────────────────

class TestLaunchRequestReconciliation:
    BASE = {
        "org_id": "o1",
        "selected_system_ids": ["salesforce"],
    }

    def test_singular_pack_id_populates_pack_ids(self):
        req = LaunchRequest(**self.BASE, pack_id="service_cloud")
        assert req.pack_id == "service_cloud"
        assert req.pack_ids == ["service_cloud"]

    def test_pack_ids_list_populates_primary_pack_id(self):
        req = LaunchRequest(**self.BASE, pack_ids=["ncino", "service_cloud"])
        assert req.pack_ids == ["ncino", "service_cloud"]
        assert req.pack_id == "ncino"  # primary is the first

    def test_both_provided_are_reconciled_and_deduped(self):
        req = LaunchRequest(
            **self.BASE, pack_id="ncino", pack_ids=["ncino", "service_cloud"]
        )
        assert req.pack_ids == ["ncino", "service_cloud"]
        assert req.pack_id == "ncino"

    def test_missing_both_without_template_raises(self):
        with pytest.raises(ValueError):
            LaunchRequest(**self.BASE)

    def test_known_template_supplies_pack(self):
        # No pack/pack_ids, but a known template can supply one → valid.
        req = LaunchRequest(org_id="o1", template_id="commercial_lending")
        assert req.pack_ids == []
        assert req.pack_id is None


# ── ComputeRequest — singular/list reconciliation ─────────────────────────────

class TestComputeRequestReconciliation:
    def test_singular_pack_populates_pack_ids(self):
        req = ComputeRequest(pack="service_cloud")
        assert req.pack == "service_cloud"
        assert req.pack_ids == ["service_cloud"]

    def test_pack_ids_list_populates_primary_pack(self):
        req = ComputeRequest(pack_ids=["ncino", "service_cloud"])
        assert req.pack == "ncino"
        assert req.pack_ids == ["ncino", "service_cloud"]

    def test_no_pack_stays_none(self):
        req = ComputeRequest()
        assert req.pack is None
        assert req.pack_ids == []

    def test_duplicates_collapse(self):
        req = ComputeRequest(pack="ncino", pack_ids=["ncino", "ncino", "service_cloud"])
        assert req.pack_ids == ["ncino", "service_cloud"]
        assert req.pack == "ncino"


# ── Launch route — persistence + single-pack parity ───────────────────────────

_SINGLE = {
    "org_id": "test_org_p1",
    "focus_id": "approvals_compliance",
    "industry_id": "public_sector",
    "selected_system_ids": ["salesforce", "jira"],
    "pack_id": "strs_benefits",
}


class TestLaunchRoutePackIds:
    def test_single_pack_response_is_byte_identical_plus_pack_ids(self, client):
        resp = client.post("/api/stack-builder/launch", headers=_auth(), json=_SINGLE)
        assert resp.status_code == 200
        data = resp.json()
        # Existing singular contract unchanged.
        assert data["packId"] == "strs_benefits"
        # New additive field: the one-element list.
        assert data["packIds"] == ["strs_benefits"]

    def test_single_pack_run_record_carries_pack_ids(self, client):
        run_id = client.post(
            "/api/stack-builder/launch", headers=_auth(), json=_SINGLE
        ).json()["runId"]
        run = client.get(f"/api/runs/{run_id}", headers=_auth()).json()
        assert run["packId"] == "strs_benefits"
        assert run["packIds"] == ["strs_benefits"]

    def test_multi_pack_selection_persists_order_preserving_deduped(self, client):
        body = {
            **_SINGLE,
            "pack_ids": ["strs_benefits", "service_cloud", "strs_benefits"],
        }
        resp = client.post("/api/stack-builder/launch", headers=_auth(), json=body)
        assert resp.status_code == 200
        data = resp.json()
        # Primary pack leads; duplicate collapses; order preserved.
        assert data["packId"] == "strs_benefits"
        assert data["packIds"] == ["strs_benefits", "service_cloud"]

        run = client.get(f"/api/runs/{data['runId']}", headers=_auth()).json()
        assert run["packIds"] == ["strs_benefits", "service_cloud"]

    def test_pack_ids_only_no_singular_is_accepted(self, client):
        body = {k: v for k, v in _SINGLE.items() if k != "pack_id"}
        body["pack_ids"] = ["ncino", "service_cloud"]
        resp = client.post("/api/stack-builder/launch", headers=_auth(), json=body)
        assert resp.status_code == 200
        data = resp.json()
        assert data["packId"] == "ncino"
        assert data["packIds"] == ["ncino", "service_cloud"]

    def test_no_pack_and_no_template_still_422(self, client):
        body = {k: v for k, v in _SINGLE.items() if k != "pack_id"}
        resp = client.post("/api/stack-builder/launch", headers=_auth(), json=body)
        assert resp.status_code == 422
