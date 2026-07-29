"""2.0-B1 T4 contract tests — the signed evidence-export API.

Covers:
  AC4 — the bundle returned by the live route verifies against its signature,
        and altering any byte of what the route served fails verification.

Plus the route contract: RBAC, tenancy, 404 vs 400, and the download form.

Drives the real routes + tenancy middleware over a live offline run, mirroring
``test_trace_graph_contract.py``. The installation's license ``report_key`` is
monkeypatched, because a test database carries no issued license — the
unpatched case is asserted too (a missing key must 400, never return an unsigned
bundle).
"""
from __future__ import annotations

import json
import os
from typing import Dict

import pytest
from fastapi.testclient import TestClient

from app import evidence_export as ee

REPORT_KEY = "rk-contract-test"


def _auth() -> Dict[str, str]:
    return {"Authorization": f"Bearer {os.getenv('DEV_JWT', 'dev-token-change-me')}"}


@pytest.fixture
def signing_key(monkeypatch):
    """Give the installation a report_key so a bundle can be signed."""
    monkeypatch.setattr(
        "app.usage_report._resolve_license_signing",
        lambda org_id: (REPORT_KEY, "cf-2026-1", org_id),
    )


@pytest.fixture(scope="module")
def exported_run_id(client: TestClient):
    """Start an offline run and wait until it materializes opportunities."""
    import time

    body = {
        "connectedSources": ["ServiceNow", "Jira & Confluence"],
        "uploadedFiles": [], "sampleWorkspaceEnabled": False,
        "mode": "offline", "systems": ["salesforce", "servicenow", "jira"],
    }
    r = client.post("/api/runs/start", headers=_auth(), json=body)
    assert r.status_code in (200, 201), f"start failed: {r.text}"
    run_id = r.json().get("runId") or r.json().get("id")
    assert run_id

    status = "running"
    for _ in range(90):
        st = client.get(f"/api/runs/{run_id}/status", headers=_auth())
        if st.status_code == 200:
            status = st.json().get("status", "running")
            if status in ("complete", "partial", "failed"):
                break
        time.sleep(1)
    assert status in ("complete", "partial"), f"run reached '{status}'"
    return run_id


@pytest.fixture(scope="module")
def first_opp_id(client: TestClient, exported_run_id):
    r = client.get(f"/api/runs/{exported_run_id}/opportunities", headers=_auth())
    assert r.status_code == 200 and r.json()
    return r.json()[0]["id"]


# ── 404 / 400 ───────────────────────────────────────────────────────────────


def test_unknown_run_is_404(client: TestClient, signing_key):
    r = client.get(
        "/api/runs/run_xyz_unknown/opportunities/opp_001/evidence-export",
        headers=_auth(),
    )
    assert r.status_code == 404


def test_unknown_opportunity_is_404(client: TestClient, exported_run_id, signing_key):
    r = client.get(
        f"/api/runs/{exported_run_id}/opportunities/opp_does_not_exist/evidence-export",
        headers=_auth(),
    )
    assert r.status_code == 404


def test_without_a_license_report_key_the_export_is_refused_not_unsigned(
    client: TestClient, exported_run_id, first_opp_id, monkeypatch
):
    """A test install has no issued license. The export must 400 with the reason
    rather than hand back an unsigned bundle."""
    r = client.get(
        f"/api/runs/{exported_run_id}/opportunities/{first_opp_id}/evidence-export",
        headers=_auth(),
    )
    assert r.status_code == 400, r.text
    assert "report_key" in r.text
    assert "signature" not in r.json()


# ── AC4 end to end ──────────────────────────────────────────────────────────


def test_ac4_finding_export_verifies_and_tampering_fails(
    client: TestClient, exported_run_id, first_opp_id, signing_key
):
    r = client.get(
        f"/api/runs/{exported_run_id}/opportunities/{first_opp_id}/evidence-export",
        headers=_auth(),
    )
    assert r.status_code == 200, r.text
    envelope = r.json()

    assert envelope["algorithm"] == ee.SIGNATURE_ALGORITHM
    body = envelope["bundle"]
    assert body["scope"] == "finding"
    assert body["run_id"] == exported_run_id
    assert body["opportunity_id"] == first_opp_id
    assert body["finding_count"] == 1
    assert body["integrity"]["content_root"]
    # Run + pack provenance is what makes the bundle re-explainable later.
    assert body["run_provenance"]["run_id"] == exported_run_id
    assert "pack_version" in body["run_provenance"]

    assert ee.verify_export_envelope(envelope, REPORT_KEY)["verified"] is True

    # AC4 — altering any byte fails.
    tampered = json.loads(json.dumps(envelope))
    tampered["bundle"]["findings"][0]["opportunity"]["impact"] = 99
    assert ee.verify_export_envelope(tampered, REPORT_KEY)["verified"] is False

    assert ee.verify_export_envelope(envelope, "rk-wrong")["verified"] is False


def test_ac4_report_export_verifies(client: TestClient, exported_run_id, signing_key):
    r = client.get(f"/api/runs/{exported_run_id}/evidence-export", headers=_auth())
    assert r.status_code == 200, r.text
    envelope = r.json()
    body = envelope["bundle"]
    assert body["scope"] == "report"
    assert body["opportunity_id"] is None
    assert body["finding_count"] >= 1
    assert "report_artifacts" in body
    assert ee.verify_export_envelope(envelope, REPORT_KEY)["verified"] is True


def test_ac4_download_form_bytes_verify_and_tampering_fails(
    client: TestClient, exported_run_id, first_opp_id, signing_key
):
    """The attachment bytes are what an auditor stores and re-verifies."""
    r = client.get(
        f"/api/runs/{exported_run_id}/opportunities/{first_opp_id}/evidence-export",
        headers=_auth(),
        params={"download": "1"},
    )
    assert r.status_code == 200, r.text
    assert "attachment" in r.headers.get("content-disposition", "")
    raw = r.content
    assert ee.verify_export_bytes(raw, REPORT_KEY)["verified"] is True

    # Flip one byte inside the serialised bundle.
    flipped = bytearray(raw)
    idx = raw.find(b'"scope"')
    assert idx > 0
    flipped[idx + 2] = (flipped[idx + 2] + 1) % 256
    assert ee.verify_export_bytes(bytes(flipped), REPORT_KEY)["verified"] is False


# ── RBAC + tenancy ──────────────────────────────────────────────────────────


def test_viewer_cannot_generate_a_signed_export(
    client: TestClient, exported_run_id, first_opp_id, signing_key, monkeypatch
):
    """The export is a distributable, audited attestation — above plain viewer."""
    monkeypatch.setenv("DEV_JWT_ROLE", "viewer")
    r = client.get(
        f"/api/runs/{exported_run_id}/opportunities/{first_opp_id}/evidence-export",
        headers=_auth(),
    )
    assert r.status_code in (401, 403), (
        f"a viewer must not be able to issue a signed export, got {r.status_code}"
    )
    assert "signature" not in r.text


def test_export_isolated_by_org(
    client: TestClient, exported_run_id, first_opp_id, signing_key, monkeypatch
):
    """The run belongs to the dev org; another org must never receive a signed
    bundle attesting to it."""
    monkeypatch.setenv("DEV_JWT_ORG", "some_other_org")
    r = client.get(
        f"/api/runs/{exported_run_id}/opportunities/{first_opp_id}/evidence-export",
        headers=_auth(),
    )
    assert r.status_code in (403, 404), (
        f"cross-org export must be denied, got {r.status_code}: {r.text}"
    )
    assert "signature" not in r.text
