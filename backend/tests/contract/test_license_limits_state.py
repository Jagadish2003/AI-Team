"""Contract tests — R17-D4 Addendum A / T10 (AT-505): Integration-Hub
license-limit state (systems used / systems licensed).

Covers AC14: the Integration Hub shows systems-used vs systems-licensed, and the
enforced count matches the pricing definition of a system (one connected entity).

The endpoint (``GET /api/license/limits``) reuses the same ``license_limits``
helpers the connect-time gate (T9) enforces with, so these tests assert that the
number the hub would SHOW is exactly the number that gets ENFORCED — they connect
systems up to the limit, read the limit-state endpoint, and confirm it agrees
with the 402 block from the connect route.

``get_current_license_status`` is monkeypatched (the same technique the T9 and run
gate tests use) so the org's ``limits.max_systems`` is driven directly, without
minting a key against the real CloudFulcrum private key. The connected-system
count runs through the real per-org connector state.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from app import db, license_limits

AUTH = {"Authorization": "Bearer dev-token-change-me"}
LIMITS_STATUS = "app.license_limits.get_current_license_status"
LIMITS_PATH = "/api/license/limits"


def _set_max_systems(monkeypatch, value) -> None:
    """Force the org's validated license to carry ``limits.max_systems = value``."""
    monkeypatch.setattr(
        LIMITS_STATUS,
        lambda *a, **k: {"status": "valid", "payload": {"limits": {"max_systems": value}}},
    )


def _fresh_org() -> str:
    return f"org_lim_{uuid.uuid4().hex[:10]}"


def _seed_catalog(connector_ids) -> None:
    """Seed shared (org-less) catalog rows so org_connector_get finds the ids."""
    for cid in connector_ids:
        db.upsert(
            "connectors",
            cid,
            {"id": cid, "name": cid.title(), "status": "disconnected"},
        )


# ===========================================================================
# Unit — pure derivation (_build_limit_state): no DB, no license needed
# ===========================================================================
def test_build_state_under_limit_has_headroom():
    assert license_limits._build_limit_state(2, 6) == {
        "systemsUsed": 2,
        "systemsLicensed": 6,
        "unlimited": False,
        "canConnectMore": True,
    }


def test_build_state_at_limit_has_no_headroom():
    """Exactly at the cap: used == licensed → no more new systems (AC10 boundary)."""
    state = license_limits._build_limit_state(6, 6)
    assert state["systemsUsed"] == 6
    assert state["systemsLicensed"] == 6
    assert state["unlimited"] is False
    assert state["canConnectMore"] is False


def test_build_state_over_limit_reports_overage_without_headroom():
    """A key whose limit is BELOW the connected count (AC12) still reports both
    counts truthfully and offers no headroom — it never fabricates capacity."""
    state = license_limits._build_limit_state(7, 6)
    assert state["systemsUsed"] == 7
    assert state["systemsLicensed"] == 6
    assert state["canConnectMore"] is False


def test_build_state_unlimited_when_max_systems_none():
    """max_systems None → unlimited: licensed null, always headroom (AC13)."""
    assert license_limits._build_limit_state(3, None) == {
        "systemsUsed": 3,
        "systemsLicensed": None,
        "unlimited": True,
        "canConnectMore": True,
    }


def test_build_state_zero_limit_has_no_headroom():
    """A zero cap is a real numeric limit (not unlimited) with no headroom."""
    state = license_limits._build_limit_state(0, 0)
    assert state["unlimited"] is False
    assert state["canConnectMore"] is False


# ===========================================================================
# Unit — get_limit_state: monkeypatched license + real connector count
# ===========================================================================
def test_get_limit_state_reflects_connected_count(monkeypatch):
    _set_max_systems(monkeypatch, 6)
    org = _fresh_org()
    _seed_catalog(["a", "b", "c"])
    db.org_connector_set(org, "a", {"id": "a", "status": "connected"})
    db.org_connector_set(org, "b", {"id": "b", "status": "connected"})
    db.org_connector_set(org, "c", {"id": "c", "status": "disconnected"})

    state = license_limits.get_limit_state(org)
    assert state == {
        "systemsUsed": 2,
        "systemsLicensed": 6,
        "unlimited": False,
        "canConnectMore": True,
    }


def test_get_limit_state_unlimited_license(monkeypatch):
    _set_max_systems(monkeypatch, None)
    org = _fresh_org()
    _seed_catalog(["a"])
    db.org_connector_set(org, "a", {"id": "a", "status": "connected"})

    state = license_limits.get_limit_state(org)
    assert state["systemsLicensed"] is None
    assert state["unlimited"] is True
    assert state["canConnectMore"] is True
    assert state["systemsUsed"] == 1


# ===========================================================================
# Endpoint — GET /api/license/limits
# ===========================================================================
def test_endpoint_returns_shape(client: TestClient, monkeypatch):
    from app.rbac import seed_owner

    org = _fresh_org()
    seed_owner(org, "dev-token-change-me")
    _set_max_systems(monkeypatch, 6)
    _seed_catalog(["sys1"])
    db.org_connector_set(org, "sys1", {"id": "sys1", "status": "connected"})
    hdr = {**AUTH, "X-Org-Id": org}

    resp = client.get(LIMITS_PATH, headers=hdr)

    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "systemsUsed": 1,
        "systemsLicensed": 6,
        "unlimited": False,
        "canConnectMore": True,
    }


def test_endpoint_unlimited_license(client: TestClient, monkeypatch):
    from app.rbac import seed_owner

    org = _fresh_org()
    seed_owner(org, "dev-token-change-me")
    _set_max_systems(monkeypatch, None)
    hdr = {**AUTH, "X-Org-Id": org}

    resp = client.get(LIMITS_PATH, headers=hdr)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["systemsLicensed"] is None
    assert body["unlimited"] is True
    assert body["canConnectMore"] is True


# ===========================================================================
# AC14 — the count the hub SHOWS is the count that is ENFORCED
# ===========================================================================
def test_ac14_shown_state_matches_enforced_block(client: TestClient, monkeypatch):
    """Connect up to the limit; the limit-state endpoint reports used == licensed
    with no headroom, and the connect route blocks the next system with the SAME
    used/licensed numbers — proving the shown count is the enforced count."""
    from app.rbac import seed_owner

    org = _fresh_org()
    seed_owner(org, "dev-token-change-me")
    _seed_catalog(["sys1", "sys2", "sys3"])
    _set_max_systems(monkeypatch, 2)
    hdr = {**AUTH, "X-Org-Id": org}

    # Connect the first two (within the licensed limit).
    assert client.post("/api/connectors/sys1/connect", json={}, headers=hdr).status_code == 200
    assert client.post("/api/connectors/sys2/connect", json={}, headers=hdr).status_code == 200

    # The hub-facing state shows we are exactly at the cap, no headroom left.
    state = client.get(LIMITS_PATH, headers=hdr).json()
    assert state == {
        "systemsUsed": 2,
        "systemsLicensed": 2,
        "unlimited": False,
        "canConnectMore": False,
    }

    # The enforced block on the (N+1)th connect carries the SAME counts.
    blocked = client.post("/api/connectors/sys3/connect", json={}, headers=hdr)
    assert blocked.status_code == 402
    detail = blocked.json()["detail"]
    assert detail["systemsUsed"] == state["systemsUsed"]
    assert detail["systemsLicensed"] == state["systemsLicensed"]


def test_ac14_state_updates_after_higher_limit_no_restart(client: TestClient, monkeypatch):
    """A higher limit (AC11) is reflected immediately by the state endpoint —
    canConnectMore flips back to True with no restart."""
    from app.rbac import seed_owner

    org = _fresh_org()
    seed_owner(org, "dev-token-change-me")
    _seed_catalog(["sys1", "sys2"])
    hdr = {**AUTH, "X-Org-Id": org}

    _set_max_systems(monkeypatch, 1)
    assert client.post("/api/connectors/sys1/connect", json={}, headers=hdr).status_code == 200
    at_limit = client.get(LIMITS_PATH, headers=hdr).json()
    assert at_limit["canConnectMore"] is False
    assert at_limit["systemsLicensed"] == 1

    # Install a key with a higher limit (simulated by the new validated status).
    _set_max_systems(monkeypatch, 3)
    after = client.get(LIMITS_PATH, headers=hdr).json()
    assert after["systemsLicensed"] == 3
    assert after["canConnectMore"] is True


# ===========================================================================
# Access — viewer+ (matches GET /api/connectors), not Owner-only
# ===========================================================================
@pytest.mark.parametrize("role", ["owner", "analyst", "viewer"])
def test_endpoint_readable_by_every_hub_role(client: TestClient, monkeypatch, role):
    """The Integration Hub is viewer+, so its usage state must be too — unlike the
    Owner-only full status route (GET /api/license)."""
    from app.rbac import _ensure_members_table
    from datetime import datetime, timezone

    _ensure_members_table()
    org = _fresh_org()
    con = db.connect()
    try:
        con.execute(
            "INSERT INTO workspace_members (org_id, user_id, role, created_at) "
            "VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (org_id, user_id) DO UPDATE SET role=EXCLUDED.role",
            (org, "dev-token-change-me", role, datetime.now(timezone.utc).isoformat()),
        )
        con.commit()
    finally:
        con.close()
    _set_max_systems(monkeypatch, 6)
    hdr = {**AUTH, "X-Org-Id": org}

    resp = client.get(LIMITS_PATH, headers=hdr)
    assert resp.status_code == 200, resp.text


def test_endpoint_requires_auth(client: TestClient):
    """No bearer token → 401 (never an unauthenticated read of license state)."""
    resp = client.get(LIMITS_PATH)
    assert resp.status_code == 401
