"""LIC-1 / T10 (AT-351) — full-lifecycle contract safety net.

This is the integration suite that locks LIC-1 behaviour behind CI. Unlike the
per-task tests (which mostly mock ``validate_license``), this suite mints REAL
keys with a throwaway Ed25519 keypair and monkeypatches the baked-in public key
so signatures actually verify — then drives the real validation core, runtime
clock guard, discovery-run gate, and admin update route end to end.

Covers the six lifecycle scenarios from the ticket plus Owner-only access:
  1. a valid key passes
  2. expired-but-within-grace -> grace (full function)
  3. past-grace -> read-only, discovery blocked, reads still viewable
  4. a tampered key -> invalid
  5. clock rollback > 2 days -> read-only + license.clock_anomaly
  6. a new key pasted via the admin route updates status
  7. Owner-only access to the license routes (analyst/viewer -> 403)

Offline by design: a throwaway keypair, no network, no dev.db (the contract
conftest provides an isolated temp DB). Maps to AC1–AC9 and AC11.
"""
from __future__ import annotations

import base64
import datetime
import json
import uuid
from datetime import datetime as _dt
from datetime import timezone

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app import db
from app.license_runtime import (
    LICENSE_KEY_KV,
    LICENSE_LAST_SEEN_KV,
    LICENSE_LAST_STATUS_KV,
    evaluate_license,
)
from app.license_runtime import get_current_license_status as real_status
from app.licensing import LicenseStatus, validate_license

AUTH = {"Authorization": "Bearer dev-token-change-me"}
DEV_USER = "dev-token-change-me"
GATE = "app.middleware.license_gate.get_current_license_status"

STATUS_PATH = "/api/license"
UPDATE_PATH = "/api/license/update-key"
BANNER_PATH = "/api/license/banner"

# One throwaway keypair for the whole module. We hold the private half so we can
# mint keys; the public half is patched in as the "baked-in" verification key.
_PRIV = Ed25519PrivateKey.generate()
_PUB = _PRIV.public_key()


def _iso(days_from_today: int) -> str:
    return (datetime.date.today() + datetime.timedelta(days=days_from_today)).isoformat()


def _mint(
    *,
    expires_at: str,
    grace_days: int = 14,
    customer: str = "City National Bank",
    license_id: str = "cnb-2026-001",
    term_months: int = 12,
) -> str:
    """Mint a real signed key using the exact issuing encoding (sort_keys + b64)."""
    payload = {
        "customer": customer,
        "license_id": license_id,
        "issued_at": _iso(-1),
        "expires_at": expires_at,
        "term_months": term_months,
        "grace_days": grace_days,
        "limits": {"max_workspaces": None, "enabled_packs": None},
    }
    payload_b64 = base64.b64encode(json.dumps(payload, sort_keys=True).encode()).decode()
    sig_b64 = base64.b64encode(_PRIV.sign(payload_b64.encode())).decode()
    return f"{payload_b64}.{sig_b64}"


@pytest.fixture(autouse=True)
def _use_throwaway_public_key(monkeypatch):
    """Verify against our throwaway public key everywhere validate_license runs
    (the route, the runtime, and the gate all funnel through load_public_key)."""
    monkeypatch.setattr("app.licensing.load_public_key", lambda *a, **k: _PUB)


def _reset_license_kv() -> None:
    db.kv_set(LICENSE_KEY_KV, None)
    db.kv_set(LICENSE_LAST_SEEN_KV, None)
    db.kv_set(LICENSE_LAST_STATUS_KV, None)


def _set_role(role: str) -> dict:
    """Put the dev user in a fresh org with the given role; return request headers."""
    from app.rbac import _ensure_members_table

    _ensure_members_table()
    org_id = f"lic_life_{uuid.uuid4().hex[:8]}"
    con = db.connect()
    try:
        con.execute(
            "INSERT INTO workspace_members (org_id, user_id, role, created_at) "
            "VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (org_id, user_id) DO UPDATE SET role=EXCLUDED.role, created_at=EXCLUDED.created_at",
            (org_id, DEV_USER, role, _dt.now(timezone.utc).isoformat()),
        )
        con.commit()
    finally:
        con.close()
    return {**AUTH, "X-Org-Id": org_id}


# ===========================================================================
# 1–4 + AC2: the pure validation core against real signed keys
# ===========================================================================
def test_valid_key_passes():
    result = validate_license(_mint(expires_at=_iso(120)), public_key=_PUB)
    assert result["status"] == LicenseStatus.VALID
    assert result["customer"] == "City National Bank"
    assert result["days_remaining"] == 120


def test_expired_within_grace_is_grace():
    result = validate_license(_mint(expires_at=_iso(-7), grace_days=14), public_key=_PUB)
    assert result["status"] == LicenseStatus.GRACE


def test_past_grace_is_readonly():
    result = validate_license(_mint(expires_at=_iso(-30), grace_days=14), public_key=_PUB)
    assert result["status"] == LicenseStatus.READONLY


def test_tampered_key_is_invalid():
    key = _mint(expires_at=_iso(-30), grace_days=14)
    payload_b64, sig_b64 = key.split(".")
    tampered = json.loads(base64.b64decode(payload_b64))
    tampered["expires_at"] = _iso(3650)  # forge a far-future expiry
    forged = (
        base64.b64encode(json.dumps(tampered, sort_keys=True).encode()).decode()
        + "."
        + sig_b64
    )
    result = validate_license(forged, public_key=_PUB)
    assert result == {"status": LicenseStatus.INVALID, "reason": "signature_or_format"}


# ===========================================================================
# 5 + AC8: clock-rollback guard via the runtime
# ===========================================================================
def test_clock_rollback_is_readonly_and_emits_anomaly(monkeypatch):
    _reset_license_kv()
    # A valid key is installed, but the stored last_seen is far in the future.
    db.kv_set(LICENSE_KEY_KV, _mint(expires_at=_iso(120)))
    db.kv_set(LICENSE_LAST_SEEN_KV, _iso(10))  # 10 days ahead of "today"

    events: list[tuple] = []
    monkeypatch.setattr(
        "app.license_runtime.record_event",
        lambda name, payload=None: events.append((name, payload)),
    )

    result = evaluate_license(today=datetime.date.today(), persist=False, emit=True)

    assert result["status"] == LicenseStatus.READONLY
    assert result["reason"] == "clock_rollback"
    assert any(name == "license.clock_anomaly" for name, _ in events)


# ===========================================================================
# 3 + AC5: past-grace blocks discovery runs but leaves reads viewable
# ===========================================================================
def test_readonly_blocks_runs_but_reads_viewable(client, monkeypatch):
    _reset_license_kv()
    db.kv_set(LICENSE_KEY_KV, _mint(expires_at=_iso(-30), grace_days=14))  # past grace
    # Point the gate at the REAL status evaluator (overriding the session
    # "always valid" fixture) so it derives read-only from the stored key.
    monkeypatch.setattr(GATE, real_status)

    blocked = client.post("/api/runs/start", json={}, headers=AUTH)
    assert blocked.status_code == 402
    assert blocked.json()["reason"] == "license_inactive"
    assert blocked.json()["licenseStatus"] == LicenseStatus.READONLY

    # Reads remain available (findings/reports/graph stay viewable in read-only).
    reads = client.get("/api/runs", headers=AUTH)
    assert reads.status_code == 200


def test_valid_key_does_not_block_runs(client, monkeypatch):
    _reset_license_kv()
    db.kv_set(LICENSE_KEY_KV, _mint(expires_at=_iso(120)))
    monkeypatch.setattr(GATE, real_status)

    resp = client.post("/api/runs/start", json={}, headers=AUTH)
    # The gate must not 402 a valid license; the route may 200/422 on the body.
    assert resp.status_code != 402


# ===========================================================================
# 6 + AC7: a new key pasted via the admin route updates status
# ===========================================================================
def test_admin_update_key_updates_status(client):
    _reset_license_kv()
    headers = _set_role("owner")
    key = _mint(expires_at=_iso(200))

    resp = client.post(UPDATE_PATH, json={"key": key}, headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == LicenseStatus.VALID
    assert db.kv_get(LICENSE_KEY_KV) == key  # validate-before-store persisted it
    # The cached status is refreshed on update so the DB matches the live status
    # immediately (no lag until the next periodic check).
    assert db.kv_get(LICENSE_LAST_STATUS_KV) == LicenseStatus.VALID

    status = client.get(STATUS_PATH, headers=headers)
    assert status.status_code == 200
    assert status.json()["status"] == LicenseStatus.VALID
    assert status.json()["customer"] == "City National Bank"


def test_admin_update_rejects_tampered_key_and_keeps_existing(client):
    _reset_license_kv()
    headers = _set_role("owner")
    good = _mint(expires_at=_iso(200))
    db.kv_set(LICENSE_KEY_KV, good)  # a working key already installed

    resp = client.post(UPDATE_PATH, json={"key": "tampered.bad"}, headers=headers)
    assert resp.status_code == 400
    assert "not valid" in resp.json()["detail"].lower()
    assert db.kv_get(LICENSE_KEY_KV) == good  # untouched


# ===========================================================================
# 7 + AC9: Owner-only access to the license routes
# ===========================================================================
@pytest.mark.parametrize("role", ["analyst", "viewer"])
def test_non_owner_forbidden_on_status(client, role):
    resp = client.get(STATUS_PATH, headers=_set_role(role))
    assert resp.status_code == 403


@pytest.mark.parametrize("role", ["analyst", "viewer"])
def test_non_owner_forbidden_on_update(client, role):
    resp = client.post(UPDATE_PATH, json={"key": _mint(expires_at=_iso(200))}, headers=_set_role(role))
    assert resp.status_code == 403


# ===========================================================================
# T9 + AC4/AC5: the expiry banner signal is readable by EVERY authenticated
# role (not just Owner), so the banner shows on every page for analysts/viewers.
# ===========================================================================
@pytest.mark.parametrize("role", ["owner", "analyst", "viewer"])
def test_banner_status_readable_by_every_role(client, role):
    """Unlike the Owner-only full status, the banner endpoint is auth-only so the
    global expiry banner renders for every role (AC4/AC5)."""
    _reset_license_kv()
    db.kv_set(LICENSE_KEY_KV, _mint(expires_at=_iso(-7), grace_days=14))  # grace

    resp = client.get(BANNER_PATH, headers=_set_role(role))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == LicenseStatus.GRACE
    assert body["expires_at"] == _iso(-7)
    # Minimal payload only — status/expires_at/reason; no Owner-only admin detail.
    assert set(body) == {"status", "expires_at", "reason"}


def test_banner_reports_no_license_reason_for_fresh_install(client):
    """AC6 / §5: a never-licensed install surfaces reason=no_license so the banner
    can say 'No valid license installed' instead of mislabelling it 'expired'."""
    _reset_license_kv()  # no key installed at all
    resp = client.get(BANNER_PATH, headers=_set_role("analyst"))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == LicenseStatus.READONLY
    assert body["reason"] == "no_license"


def test_banner_requires_authentication(client):
    resp = client.get(BANNER_PATH)  # no Authorization header
    assert resp.status_code == 401
