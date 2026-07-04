"""Contract tests — R17-D4 Addendum A / T12 (§2 "Dynamic Organisation Name").

Covers the single organisation display-name resolver (``app.org_display_name``)
and its endpoint ``GET /api/license/org-name``:

  * AC15 — after a key is installed the ``org_name`` from the license payload is
    what the endpoint returns, and pasting a key with a different ``org_name``
    updates the response immediately (a live, side-effect-free read; no restart).
  * AC16 — before any key is installed (and for any non-verifiable license state)
    the endpoint returns a neutral default, never a stale or placeholder customer
    name.

Two layers, mirroring the T10 limits tests:
  * Pure unit tests of the resolution rule (``_display_name_from_result``) — no DB
    and no license needed.
  * Resolver + endpoint tests with ``get_current_license_status`` monkeypatched
    (the same technique the T6 / T9 / T10 tests use) so the org's validated payload
    is driven directly, without minting a key against the real CloudFulcrum private
    key.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app import db
from app.org_display_name import (
    DEFAULT_ORG_DISPLAY_NAME,
    _display_name_from_result,
    resolve_org_display_name,
)

AUTH = {"Authorization": "Bearer dev-token-change-me"}
DEV_USER = "dev-token-change-me"
ORG_NAME_PATH = "/api/license/org-name"

# ``get_current_license_status`` is imported INTO org_display_name; patch it there
# so both the resolver and the endpoint (which calls the resolver) see the stub.
STATUS_IN_RESOLVER = "app.org_display_name.get_current_license_status"


def _installed(org_name=None, customer=None, status="valid") -> dict:
    """A validated-license result dict shaped as ``get_current_license_status``
    returns for a verified key — i.e. carrying a ``payload``."""
    payload: dict = {}
    if org_name is not None:
        payload["org_name"] = org_name
    if customer is not None:
        payload["customer"] = customer
    return {"status": status, "payload": payload}


# ===========================================================================
# Unit — pure resolution rule (_display_name_from_result): no DB / no license
# ===========================================================================
def test_prefers_org_name_over_customer():
    result = _installed(org_name="Teachers CU", customer="Teachers Credit Union")
    assert _display_name_from_result(result) == "Teachers CU"


def test_falls_back_to_customer_when_no_org_name():
    """Pre-addendum keys carry no org_name; the real customer name is correct
    there (not stale), so the resolver falls back to it rather than the default."""
    assert _display_name_from_result(_installed(customer="City National Bank")) == "City National Bank"


def test_neutral_default_when_no_payload():
    """AC16: no verified key (no payload) → neutral default, never a customer name."""
    assert _display_name_from_result({"status": "readonly", "reason": "no_license"}) == DEFAULT_ORG_DISPLAY_NAME
    assert _display_name_from_result({"status": "invalid", "reason": "signature_or_format"}) == DEFAULT_ORG_DISPLAY_NAME
    assert _display_name_from_result({"status": "readonly", "reason": "clock_rollback"}) == DEFAULT_ORG_DISPLAY_NAME
    assert _display_name_from_result({}) == DEFAULT_ORG_DISPLAY_NAME


def test_blank_names_fall_through_to_default():
    """Whitespace-only org_name and customer are not usable names → default."""
    assert _display_name_from_result(_installed(org_name="   ", customer="")) == DEFAULT_ORG_DISPLAY_NAME


def test_blank_org_name_falls_back_to_customer():
    assert _display_name_from_result(_installed(org_name="  ", customer="ACME Bank")) == "ACME Bank"


def test_name_is_trimmed():
    assert _display_name_from_result(_installed(org_name="  Teachers CU  ")) == "Teachers CU"


def test_non_string_name_ignored():
    """Defensive: a non-string org_name (a structurally bad payload) is skipped in
    favour of the next usable candidate, never rendered."""
    assert _display_name_from_result({"payload": {"org_name": 123, "customer": "ACME"}}) == "ACME"


def test_expired_readonly_key_still_shows_name():
    """A past-grace (read-only) key still carries a payload, so the org identity
    still shows — the neutral default is only for the *no-key* case, not expiry."""
    result = _installed(org_name="Teachers CU", status="readonly")
    assert _display_name_from_result(result) == "Teachers CU"


def test_neutral_default_is_generic_not_a_customer():
    """AC16: the default is a generic, British-spelled placeholder — locking the
    value so a fresh install can never surface a customer/placeholder identity."""
    assert DEFAULT_ORG_DISPLAY_NAME == "Your Organisation"


# ===========================================================================
# Resolver — resolve_org_display_name with monkeypatched license status
# ===========================================================================
def test_resolver_reads_org_name_from_license(monkeypatch):
    monkeypatch.setattr(STATUS_IN_RESOLVER, lambda *a, **k: _installed(org_name="Teachers CU"))
    assert resolve_org_display_name("org_x") == "Teachers CU"


def test_resolver_default_before_key_installed(monkeypatch):
    """AC16: before any key is installed the org evaluates to no_license (no payload)."""
    monkeypatch.setattr(STATUS_IN_RESOLVER, lambda *a, **k: {"status": "readonly", "reason": "no_license"})
    assert resolve_org_display_name("org_x") == DEFAULT_ORG_DISPLAY_NAME


def test_resolver_never_raises(monkeypatch):
    """A status-read failure degrades to the neutral default — a name read must
    never break a page render."""

    def _boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(STATUS_IN_RESOLVER, _boom)
    assert resolve_org_display_name("org_x") == DEFAULT_ORG_DISPLAY_NAME


def test_resolver_reflects_new_key_immediately(monkeypatch):
    """AC15: the resolver reads live, so a newly pasted key with a different
    org_name is reflected with no restart."""
    monkeypatch.setattr(STATUS_IN_RESOLVER, lambda *a, **k: _installed(org_name="Old Name"))
    assert resolve_org_display_name("org_x") == "Old Name"
    monkeypatch.setattr(STATUS_IN_RESOLVER, lambda *a, **k: _installed(org_name="New Name"))
    assert resolve_org_display_name("org_x") == "New Name"


# ===========================================================================
# Endpoint — GET /api/license/org-name
# ===========================================================================
def test_endpoint_returns_org_name(client: TestClient, monkeypatch):
    monkeypatch.setattr(STATUS_IN_RESOLVER, lambda *a, **k: _installed(org_name="Teachers CU"))
    resp = client.get(ORG_NAME_PATH, headers=AUTH)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"orgName": "Teachers CU"}


def test_endpoint_neutral_default_before_key(client: TestClient, monkeypatch):
    """AC16: before a key is installed the endpoint returns the neutral default."""
    monkeypatch.setattr(STATUS_IN_RESOLVER, lambda *a, **k: {"status": "readonly", "reason": "no_license"})
    resp = client.get(ORG_NAME_PATH, headers=AUTH)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"orgName": DEFAULT_ORG_DISPLAY_NAME}


def test_endpoint_updates_after_new_key_no_restart(client: TestClient, monkeypatch):
    """AC15: pasting a key with a different org_name updates the endpoint response
    immediately (same running client, no restart)."""
    monkeypatch.setattr(STATUS_IN_RESOLVER, lambda *a, **k: _installed(org_name="City National Bank"))
    first = client.get(ORG_NAME_PATH, headers=AUTH)
    assert first.status_code == 200, first.text
    assert first.json() == {"orgName": "City National Bank"}

    monkeypatch.setattr(STATUS_IN_RESOLVER, lambda *a, **k: _installed(org_name="City National Trust"))
    second = client.get(ORG_NAME_PATH, headers=AUTH)
    assert second.json() == {"orgName": "City National Trust"}


@pytest.mark.parametrize("role", ["owner", "analyst", "viewer"])
def test_endpoint_readable_by_every_role(client: TestClient, monkeypatch, role):
    """Auth-only (like the banner): every role sees the org name, since the header
    and workspace labels render for all users — not just Owner."""
    from app.rbac import _ensure_members_table

    _ensure_members_table()
    org = f"org_name_{uuid.uuid4().hex[:8]}"
    con = db.connect()
    try:
        con.execute(
            "INSERT INTO workspace_members (org_id, user_id, role, created_at) "
            "VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (org_id, user_id) DO UPDATE SET role=EXCLUDED.role",
            (org, DEV_USER, role, datetime.now(timezone.utc).isoformat()),
        )
        con.commit()
    finally:
        con.close()
    monkeypatch.setattr(STATUS_IN_RESOLVER, lambda *a, **k: _installed(org_name="Teachers CU"))

    resp = client.get(ORG_NAME_PATH, headers={**AUTH, "X-Org-Id": org})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"orgName": "Teachers CU"}


def test_endpoint_requires_auth(client: TestClient):
    """No bearer token → 401 (never an unauthenticated read of license state)."""
    resp = client.get(ORG_NAME_PATH)
    assert resp.status_code == 401
