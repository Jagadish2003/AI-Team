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

import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app import db
from app.license_runtime import LICENSE_KEY_KV

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
            (org_id, DEV_USER, role, datetime.now(timezone.utc).isoformat()),
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
        "expires_at": "2027-06-18",
        "days_remaining": 300,
    }


# --------------------------------------------------------------------------
# Owner POST valid key → stores and refreshes (AC7)
# --------------------------------------------------------------------------
def test_owner_post_valid_key_stores_and_refreshes(client: TestClient, monkeypatch):
    monkeypatch.setattr(
        "app.routes_license.validate_license", lambda *a, **k: dict(_VALID_RESULT)
    )
    headers = _set_role("owner")
    key = f"valid-key-{uuid.uuid4().hex}"
    db.kv_set(LICENSE_KEY_KV, None)  # clean slate

    resp = client.post(UPDATE_PATH, json={"key": key}, headers=headers)

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "valid"
    assert resp.json()["term"] == 12
    # The valid key was persisted to the shared kv slot the validator reads.
    assert db.kv_get(LICENSE_KEY_KV) == key


# --------------------------------------------------------------------------
# Owner POST invalid key → 400, stores nothing
# --------------------------------------------------------------------------
def test_owner_post_invalid_key_rejected_stores_nothing(client: TestClient, monkeypatch):
    monkeypatch.setattr(
        "app.routes_license.validate_license",
        lambda *a, **k: {"status": "invalid", "reason": "signature_or_format"},
    )
    headers = _set_role("owner")
    existing = f"good-key-{uuid.uuid4().hex}"
    db.kv_set(LICENSE_KEY_KV, existing)  # a working key already installed

    resp = client.post(UPDATE_PATH, json={"key": "tampered.bad"}, headers=headers)

    assert resp.status_code == 400
    assert "not valid" in resp.json()["detail"].lower()
    # The previously working key must be untouched.
    assert db.kv_get(LICENSE_KEY_KV) == existing


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
