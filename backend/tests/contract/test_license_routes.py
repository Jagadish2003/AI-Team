"""Contract tests for LIC-1 / T6 (AT-347) — admin license routes.

Covers:
  * Owner GET /api/license returns the status shape (AC9 owner-allowed, AC7 read).
  * Owner POST /api/license/update-key with a valid key stores it and refreshes.
  * Owner POST with an invalid key is rejected (400) and stores nothing
    (validate-before-store — a bad paste never replaces a working key).
  * Analyst and Viewer receive 403 on both endpoints (AC9).

``validate_license`` / ``get_current_license_status`` are monkeypatched so the
routes are exercised without minting a key against the real CloudFulcrum
private key.
"""
from __future__ import annotations

import base64
import datetime
import json
import uuid
from datetime import datetime as _datetime, timezone

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from app import db
from app.license_runtime import read_org_license, set_org_license_key

AUTH = {"Authorization": "Bearer dev-token-change-me"}
DEV_USER = "dev-token-change-me"

STATUS_PATH = "/api/license"
UPDATE_PATH = "/api/license/update-key"

_VALID_RESULT = {
    "status": "valid",
    "customer": "City National Bank",
    "expires_at": "2027-06-18",
    "days_remaining": 300,
    "payload": {"term_months": 12, "customer": "City National Bank"},
}


def _pub_pem(priv: Ed25519PrivateKey) -> str:
    return priv.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()


def _mint(
    priv: Ed25519PrivateKey,
    *,
    expires_at: str,
    term_months: int = 12,
    grace_days: int = 14,
    deployment_type: str | None = None,
    org_id: str | None = None,
) -> str:
    payload = {
        "customer": "City National Bank",
        "license_id": "cnb-2026-001",
        "issued_at": "2026-01-01",
        "expires_at": expires_at,
        "term_months": term_months,
        "grace_days": grace_days,
        "limits": {"max_workspaces": None, "enabled_packs": None},
    }
    if deployment_type is not None:
        payload["deployment_type"] = deployment_type
    if org_id is not None:
        payload["org_id"] = org_id
    payload_b64 = base64.b64encode(json.dumps(payload, sort_keys=True).encode()).decode()
    sig_b64 = base64.b64encode(priv.sign(payload_b64.encode())).decode()
    return f"{payload_b64}.{sig_b64}"


def _future(days: int = 200) -> str:
    return (datetime.date.today() + datetime.timedelta(days=days)).isoformat()


def _set_role(role: str) -> dict:
    """Put the dev user in a freshly seeded org with the given role; return headers."""
    from app.rbac import _ensure_members_table

    _ensure_members_table()
    org_id = f"lic_role_{uuid.uuid4().hex[:8]}"
    con = db.connect()
    try:
        con.execute(
            "INSERT INTO workspace_members (org_id, user_id, role, created_at) "
            "VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (org_id, user_id) DO UPDATE SET role=EXCLUDED.role, created_at=EXCLUDED.created_at",
            (org_id, DEV_USER, role, _datetime.now(timezone.utc).isoformat()),
        )
        con.commit()
    finally:
        con.close()
    return {**AUTH, "X-Org-Id": org_id}


# --------------------------------------------------------------------------
# Owner GET status
# --------------------------------------------------------------------------
def test_owner_get_status_returns_shape(client: TestClient, monkeypatch):
    monkeypatch.setattr(
        "app.routes_license.get_current_license_status", lambda *a, **k: dict(_VALID_RESULT)
    )
    headers = _set_role("owner")

    resp = client.get(STATUS_PATH, headers=headers)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {
        "status": "valid",
        "customer": "City National Bank",
        "term": 12,
        # R-1.9.1-L1 / T1 (AC5): deployment_type is part of the status shape; None
        # here since _VALID_RESULT models a pre-v2 result carrying no such field.
        "deployment_type": None,
        "expires_at": "2027-06-18",
        "days_remaining": 300,
        # R-1.9.1-L1 / T2 (AC1): reason is part of the shape; null on a healthy status.
        "reason": None,
    }


# --------------------------------------------------------------------------
# Owner POST valid key → stores and refreshes (AC7)
#
# Exercises REAL Ed25519 verification end-to-end: the key is minted with a
# throwaway private key and the route trusts the matching public key via the
# LICENSE_PUBLIC_KEY env override — validate_license is NOT mocked, so the
# security-critical verify path runs for real (AC1).
# --------------------------------------------------------------------------
def test_owner_post_valid_key_stores_and_refreshes(client: TestClient, monkeypatch):
    priv = Ed25519PrivateKey.generate()
    monkeypatch.setenv("LICENSE_PUBLIC_KEY", _pub_pem(priv))
    headers = _set_role("owner")
    org_id = headers["X-Org-Id"]
    key = _mint(priv, expires_at=_future())

    resp = client.post(UPDATE_PATH, json={"key": key}, headers=headers)

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "valid"
    assert resp.json()["term"] == 12
    # The valid key was persisted to THIS org's row in org_licenses.
    assert read_org_license(org_id)["license_key"] == key


# --------------------------------------------------------------------------
# R-1.9.1-L1 / T1 (AC5): a v2 key's deployment_type reaches the status API.
# Real Ed25519 verification (validate_license not mocked): install the key, then
# GET /api/license and assert deployment_type is exposed.
# --------------------------------------------------------------------------
def test_owner_status_exposes_deployment_type(client: TestClient, monkeypatch):
    priv = Ed25519PrivateKey.generate()
    monkeypatch.setenv("LICENSE_PUBLIC_KEY", _pub_pem(priv))
    headers = _set_role("owner")
    key = _mint(priv, expires_at=_future(), deployment_type="customer_hosted")

    install = client.post(UPDATE_PATH, json={"key": key}, headers=headers)
    assert install.status_code == 200, install.text
    assert install.json()["deployment_type"] == "customer_hosted"

    status = client.get(STATUS_PATH, headers=headers)
    assert status.status_code == 200, status.text
    assert status.json()["deployment_type"] == "customer_hosted"


# --------------------------------------------------------------------------
# R-1.9.1-L1 / T2 (AC1): a signature-valid key bound to a DIFFERENT org is
# rejected at paste time with the plain-language org-mismatch reason, and the
# org's previously stored key is left untouched. Customer A's key pasted into
# Customer B's installation fails closed and says why.
# --------------------------------------------------------------------------
def test_owner_post_org_mismatch_rejected_stores_nothing(client: TestClient, monkeypatch):
    priv = Ed25519PrivateKey.generate()
    monkeypatch.setenv("LICENSE_PUBLIC_KEY", _pub_pem(priv))
    headers = _set_role("owner")
    org_id = headers["X-Org-Id"]  # this installation's org
    existing = f"good-key-{uuid.uuid4().hex}"
    set_org_license_key(org_id, existing)  # B already has a working key

    # A key issued for a DIFFERENT org, signed by the trusted key.
    foreign = _mint(priv, expires_at=_future(), org_id="some-other-org")

    resp = client.post(UPDATE_PATH, json={"key": foreign}, headers=headers)

    assert resp.status_code == 400, resp.text
    assert "different organisation" in resp.json()["detail"].lower()
    # B's working key must be untouched (validate-before-store + org binding).
    assert read_org_license(org_id)["license_key"] == existing


def test_owner_post_org_match_accepted(client: TestClient, monkeypatch):
    """A v2 key bound to THIS installation's org installs normally (AC1)."""
    priv = Ed25519PrivateKey.generate()
    monkeypatch.setenv("LICENSE_PUBLIC_KEY", _pub_pem(priv))
    headers = _set_role("owner")
    org_id = headers["X-Org-Id"]
    key = _mint(priv, expires_at=_future(), org_id=org_id)  # bound to THIS org

    resp = client.post(UPDATE_PATH, json={"key": key}, headers=headers)

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "valid"
    assert read_org_license(org_id)["license_key"] == key


def test_owner_status_surfaces_org_mismatch_reason(client: TestClient, monkeypatch):
    """AC1: an installed key bound to a different org surfaces on GET /api/license
    as status=invalid, reason=org_mismatch, so the UI can explain it.

    The key is installed directly (bypassing the paste-time guard) to model a key
    that becomes org-mismatched, then the live-validated status is read back."""
    priv = Ed25519PrivateKey.generate()
    monkeypatch.setenv("LICENSE_PUBLIC_KEY", _pub_pem(priv))
    headers = _set_role("owner")
    org_id = headers["X-Org-Id"]
    foreign = _mint(priv, expires_at=_future(), org_id="some-other-org")
    set_org_license_key(org_id, foreign)  # installed, but bound to the wrong org

    resp = client.get(STATUS_PATH, headers=headers)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "invalid"
    assert body["reason"] == "org_mismatch"


# --------------------------------------------------------------------------
# Owner POST key signed by the WRONG keypair → 400, stores nothing.
# Real signature rejection (AC1/AC2) — no mock of validate_license.
# --------------------------------------------------------------------------
def test_owner_post_wrong_signer_rejected_stores_nothing(client: TestClient, monkeypatch):
    trusted = Ed25519PrivateKey.generate()
    attacker = Ed25519PrivateKey.generate()
    monkeypatch.setenv("LICENSE_PUBLIC_KEY", _pub_pem(trusted))
    headers = _set_role("owner")
    org_id = headers["X-Org-Id"]
    existing = f"good-key-{uuid.uuid4().hex}"
    set_org_license_key(org_id, existing)  # a working key already installed

    forged = _mint(attacker, expires_at=_future())  # signed by the wrong key

    resp = client.post(UPDATE_PATH, json={"key": forged}, headers=headers)

    assert resp.status_code == 400
    assert "not valid" in resp.json()["detail"].lower()
    # The previously working key must be untouched.
    assert read_org_license(org_id)["license_key"] == existing


# --------------------------------------------------------------------------
# Owner POST a key whose payload was edited after signing → 400 (AC2).
# Real tamper detection: extend expires_at but keep the original signature.
# --------------------------------------------------------------------------
def test_owner_post_tampered_payload_rejected(client: TestClient, monkeypatch):
    trusted = Ed25519PrivateKey.generate()
    monkeypatch.setenv("LICENSE_PUBLIC_KEY", _pub_pem(trusted))
    headers = _set_role("owner")
    org_id = headers["X-Org-Id"]
    existing = f"good-key-{uuid.uuid4().hex}"
    set_org_license_key(org_id, existing)

    payload_b64, sig_b64 = _mint(trusted, expires_at=_future(10)).split(".")
    payload = json.loads(base64.b64decode(payload_b64))
    payload["expires_at"] = "2099-01-01"  # forge a longer term, reuse old signature
    forged_b64 = base64.b64encode(json.dumps(payload, sort_keys=True).encode()).decode()
    tampered = f"{forged_b64}.{sig_b64}"

    resp = client.post(UPDATE_PATH, json={"key": tampered}, headers=headers)

    assert resp.status_code == 400
    assert read_org_license(org_id)["license_key"] == existing


# --------------------------------------------------------------------------
# AC9: Analyst / Viewer are forbidden on both endpoints
# --------------------------------------------------------------------------
@pytest.mark.parametrize("role", ["analyst", "viewer"])
def test_non_owner_forbidden_get(client: TestClient, role):
    headers = _set_role(role)
    resp = client.get(STATUS_PATH, headers=headers)
    assert resp.status_code == 403
    assert resp.json()["detail"] == "Insufficient role"


@pytest.mark.parametrize("role", ["analyst", "viewer"])
def test_non_owner_forbidden_update(client: TestClient, role):
    headers = _set_role(role)
    resp = client.post(UPDATE_PATH, json={"key": "anything"}, headers=headers)
    assert resp.status_code == 403
    assert resp.json()["detail"] == "Insufficient role"
