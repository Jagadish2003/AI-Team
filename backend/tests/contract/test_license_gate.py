"""Contract tests for LIC-1 / T5 (AT-346) — discovery-run license gate.

Verifies (AC5/AC6):
  * run-start (and other run triggers) return the structured gated error when
    the license is read-only / invalid,
  * reads (e.g. GET /api/runs) stay 200 in read-only,
  * login stays 200 in read-only,
  * valid / grace never restrict, and
  * a status-check error fails CLOSED for runs but reads remain open.

The license status is controlled by monkeypatching the gate's
``get_current_license_status`` so the gate's behaviour is tested directly,
without needing the real CloudFulcrum private key to mint a 'valid' key.
"""
from __future__ import annotations

import uuid

import pytest

from app.licensing import LicenseStatus

AUTH = {"Authorization": "Bearer dev-token-change-me"}
GATE = "app.middleware.license_gate.get_current_license_status"


def _force_status(monkeypatch, status: str) -> None:
    monkeypatch.setattr(GATE, lambda *a, **k: {"status": status})


# --------------------------------------------------------------------------
# AC5: run triggers are blocked in read-only / invalid
# --------------------------------------------------------------------------
@pytest.mark.parametrize("status", [LicenseStatus.READONLY, LicenseStatus.INVALID])
def test_run_start_blocked_when_not_active(client, monkeypatch, status):
    _force_status(monkeypatch, status)

    resp = client.post("/api/runs/start", json={}, headers=AUTH)

    assert resp.status_code == 402
    body = resp.json()
    assert body["reason"] == "license_inactive"
    assert body["licenseStatus"] == status


def test_compute_blocked_in_readonly(client, monkeypatch):
    _force_status(monkeypatch, LicenseStatus.READONLY)

    resp = client.post("/api/runs/run-123/compute", json={}, headers=AUTH)

    assert resp.status_code == 402
    assert resp.json()["reason"] == "license_inactive"


def test_stack_builder_launch_blocked_in_readonly(client, monkeypatch):
    _force_status(monkeypatch, LicenseStatus.READONLY)

    resp = client.post("/api/stack-builder/launch", json={}, headers=AUTH)

    assert resp.status_code == 402


# --------------------------------------------------------------------------
# AC5: reads stay available in read-only
# --------------------------------------------------------------------------
def test_read_runs_list_allowed_in_readonly(client, monkeypatch):
    _force_status(monkeypatch, LicenseStatus.READONLY)

    resp = client.get("/api/runs", headers=AUTH)

    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


# --------------------------------------------------------------------------
# AC5/AC6: login stays available in read-only
# --------------------------------------------------------------------------
def test_login_allowed_in_readonly(client, monkeypatch):
    _force_status(monkeypatch, LicenseStatus.READONLY)

    email = f"gate-{uuid.uuid4().hex[:10]}@example.com"
    password = "Str0ng!Passw0rd123"
    reg = client.post(
        "/api/auth/register",
        json={"org_name": f"Org {uuid.uuid4().hex[:6]}", "email": email, "password": password},
    )
    assert reg.status_code == 201, reg.text

    # AUTH-2: approve the org (simulated admin step) so login is permitted.
    from auth_helpers import activate_org_by_email
    activate_org_by_email(email)

    resp = client.post("/api/auth/login", json={"email": email, "password": password})

    assert resp.status_code == 200, resp.text
    assert "token" in resp.json()


# --------------------------------------------------------------------------
# valid / grace never restrict (gate passes the request through to the route)
# --------------------------------------------------------------------------
@pytest.mark.parametrize("status", [LicenseStatus.VALID, LicenseStatus.GRACE])
def test_run_start_not_gated_when_active(client, monkeypatch, status):
    _force_status(monkeypatch, status)

    resp = client.post("/api/runs/start", json={}, headers=AUTH)

    # The gate must not block; the request reaches the route (which may 422 on
    # the empty body or 200). The only forbidden outcome here is the gate's 402.
    assert resp.status_code != 402


# --------------------------------------------------------------------------
# Failure policy: status-check error fails CLOSED for runs, open for reads.
# --------------------------------------------------------------------------
def test_run_start_fails_closed_on_status_error(client, monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("kv unavailable")

    monkeypatch.setattr(GATE, _boom)

    resp = client.post("/api/runs/start", json={}, headers=AUTH)

    assert resp.status_code == 402
    assert resp.json()["reason"] == "license_inactive"


def test_reads_unaffected_when_status_error(client, monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("kv unavailable")

    monkeypatch.setattr(GATE, _boom)

    # Reads never invoke the gate's status check, so they stay open (fail-open).
    resp = client.get("/api/runs", headers=AUTH)

    assert resp.status_code == 200
