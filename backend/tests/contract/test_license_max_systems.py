"""Contract tests — R17-D4 Addendum A / T9: enforce limits.max_systems.

Covers the Scoped Activation acceptance criteria (Addendum A §4):

  * AC10 — with max_systems = N, the first N connections succeed and the
    (N+1)th is blocked with a clear message + request path.
  * AC11 — installing a key with a HIGHER limit immediately allows more
    connections, with no restart.
  * AC12 — a key whose limit is LOWER than the currently-connected count never
    disconnects existing systems; it only blocks NEW ones (reconnecting an
    existing system still works — forward-only).
  * AC13 — max_systems = null behaves as unlimited (backwards-compatible with
    keys issued before the addendum).

The org's licensed limit is driven by monkeypatching
``app.license_limits.get_current_license_status`` (the same technique the run
gate's tests use for status), so these tests exercise the enforcement logic and
the connect routes directly without needing the real CloudFulcrum private key.
The connected-system count is driven through the real per-org connector state.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from app import db, license_limits

AUTH = {"Authorization": "Bearer dev-token-change-me"}
LIMITS_STATUS = "app.license_limits.get_current_license_status"


def _set_max_systems(monkeypatch, value) -> None:
    """Force the org's validated license to carry ``limits.max_systems = value``."""
    monkeypatch.setattr(
        LIMITS_STATUS,
        lambda *a, **k: {"status": "valid", "payload": {"limits": {"max_systems": value}}},
    )


def _set_no_license(monkeypatch) -> None:
    """Force the org to have NO verifiable license payload (unlicensed).

    R-1.9.1-L1 / T5 (AC4): this is the state that triggers the unlicensed cap —
    ``get_current_license_status`` returns a status with no ``payload`` (the shape
    a keyless / invalid install produces)."""
    monkeypatch.setattr(
        LIMITS_STATUS,
        lambda *a, **k: {"status": "readonly", "reason": "no_license"},
    )


def _fresh_org() -> str:
    return f"org_ms_{uuid.uuid4().hex[:10]}"


def _seed_catalog(connector_ids) -> None:
    """Seed shared (org-less) catalog rows so org_connector_get finds the ids."""
    for cid in connector_ids:
        db.upsert(
            "connectors",
            cid,
            {"id": cid, "name": cid.title(), "status": "disconnected"},
        )


# ===========================================================================
# Unit — the counting + entitlement helpers
# ===========================================================================
def test_get_max_systems_unlicensed_cap_without_payload(monkeypatch):
    """R-1.9.1-L1 / T5 (AC4): no verifiable payload (no_license / invalid) →
    the unlicensed cap (default 2), NOT unlimited (the pre-T5 behaviour)."""
    _set_no_license(monkeypatch)
    assert license_limits.get_max_systems("any_org") == 2
    assert license_limits.DEFAULT_UNLICENSED_SYSTEM_CAP == 2


def test_get_max_systems_unlicensed_cap_for_invalid_key(monkeypatch):
    """An installed-but-invalid key (e.g. unsupported_payload_version) has no
    usable payload, so it is treated as unlicensed → the cap, not unlimited."""
    monkeypatch.setattr(
        LIMITS_STATUS,
        lambda *a, **k: {"status": "invalid", "reason": "unsupported_payload_version"},
    )
    assert license_limits.get_max_systems("any_org") == 2


def test_get_max_systems_reads_payload(monkeypatch):
    _set_max_systems(monkeypatch, 6)
    assert license_limits.get_max_systems("any_org") == 6


def test_get_max_systems_non_integer_is_unlimited(monkeypatch):
    """A malformed limit is treated as unlimited — forward-only never over-blocks."""
    _set_max_systems(monkeypatch, "not-a-number")
    assert license_limits.get_max_systems("any_org") is None


def test_count_connected_systems_counts_only_connected():
    org = _fresh_org()
    _seed_catalog(["a", "b", "c"])
    db.org_connector_set(org, "a", {"id": "a", "status": "connected"})
    db.org_connector_set(org, "b", {"id": "b", "status": "connected"})
    db.org_connector_set(org, "c", {"id": "c", "status": "disconnected"})
    assert license_limits.count_connected_systems(org) == 2


def test_can_connect_unlimited_when_no_limit(monkeypatch):
    _set_max_systems(monkeypatch, None)
    org = _fresh_org()
    _seed_catalog(["a", "b"])
    db.org_connector_set(org, "a", {"id": "a", "status": "connected"})
    db.org_connector_set(org, "b", {"id": "b", "status": "connected"})
    assert license_limits.can_connect_new_system(org, "new_one") is True


def test_reconnect_existing_allowed_even_over_limit(monkeypatch):
    """Forward-only: re-authorising an already-connected system is not a new one."""
    _set_max_systems(monkeypatch, 1)
    org = _fresh_org()
    _seed_catalog(["a", "b"])
    db.org_connector_set(org, "a", {"id": "a", "status": "connected"})
    # At the limit (1 connected). A brand new system is blocked...
    assert license_limits.can_connect_new_system(org, "b") is False
    # ...but reconnecting the already-connected "a" is always allowed.
    assert license_limits.can_connect_new_system(org, "a") is True


# ===========================================================================
# AC10 — first N connect normally, the (N+1)th is blocked with a clear message
# ===========================================================================
def test_ac10_nth_plus_one_connection_blocked(client: TestClient, monkeypatch):
    from app.rbac import seed_owner

    org = _fresh_org()
    seed_owner(org, "dev-token-change-me")
    _seed_catalog(["sys1", "sys2", "sys3"])
    _set_max_systems(monkeypatch, 2)
    hdr = {**AUTH, "X-Org-Id": org}

    # First 2 connect normally.
    assert client.post("/api/connectors/sys1/connect", json={}, headers=hdr).status_code == 200
    assert client.post("/api/connectors/sys2/connect", json={}, headers=hdr).status_code == 200

    # The 3rd (N+1) is blocked with a clear message + request path.
    resp = client.post("/api/connectors/sys3/connect", json={}, headers=hdr)
    assert resp.status_code == 402
    detail = resp.json()["detail"]
    assert detail["reason"] == license_limits.BLOCK_REASON
    assert detail["systemsUsed"] == 2
    assert detail["systemsLicensed"] == 2
    assert "Contact CloudFulcrum" in detail["detail"]
    assert "2 systems" in detail["detail"]

    # The block never severed the existing connections.
    listed = {c["id"]: c for c in db.org_connectors_list(org)}
    assert listed["sys1"]["status"] == "connected"
    assert listed["sys2"]["status"] == "connected"
    assert listed["sys3"]["status"] != "connected"


# ===========================================================================
# AC11 — a higher limit immediately allows more connections, no restart
# ===========================================================================
def test_ac11_higher_limit_immediately_allows_more(client: TestClient, monkeypatch):
    from app.rbac import seed_owner

    org = _fresh_org()
    seed_owner(org, "dev-token-change-me")
    _seed_catalog(["sys1", "sys2", "sys3"])
    hdr = {**AUTH, "X-Org-Id": org}

    _set_max_systems(monkeypatch, 2)
    assert client.post("/api/connectors/sys1/connect", json={}, headers=hdr).status_code == 200
    assert client.post("/api/connectors/sys2/connect", json={}, headers=hdr).status_code == 200
    assert client.post("/api/connectors/sys3/connect", json={}, headers=hdr).status_code == 402

    # Install a key with a higher limit (simulated by the new validated status).
    _set_max_systems(monkeypatch, 3)
    # No restart — the very next attempt succeeds.
    assert client.post("/api/connectors/sys3/connect", json={}, headers=hdr).status_code == 200


# ===========================================================================
# AC12 — a lower limit never disconnects; it only blocks new connections
# ===========================================================================
def test_ac12_lower_limit_never_disconnects_only_blocks_new(client: TestClient, monkeypatch):
    from app.rbac import seed_owner

    org = _fresh_org()
    seed_owner(org, "dev-token-change-me")
    _seed_catalog(["sys1", "sys2", "sys3", "sys4"])
    hdr = {**AUTH, "X-Org-Id": org}

    # Three systems already connected under a generous key.
    for cid in ("sys1", "sys2", "sys3"):
        db.org_connector_set(org, cid, {"id": cid, "status": "connected"})

    # A renewed key arrives carrying a LOWER limit (2) than the 3 connected.
    _set_max_systems(monkeypatch, 2)

    # Existing connections are untouched — never auto-disconnected.
    listed = {c["id"]: c for c in db.org_connectors_list(org)}
    assert listed["sys1"]["status"] == "connected"
    assert listed["sys2"]["status"] == "connected"
    assert listed["sys3"]["status"] == "connected"

    # A brand new system is blocked (already over the reduced limit).
    resp = client.post("/api/connectors/sys4/connect", json={}, headers=hdr)
    assert resp.status_code == 402
    assert resp.json()["detail"]["reason"] == license_limits.BLOCK_REASON

    # Re-connecting an existing system still works (forward-only, idempotent).
    assert client.post("/api/connectors/sys1/connect", json={}, headers=hdr).status_code == 200


# ===========================================================================
# AC13 — max_systems null behaves as unlimited (pre-addendum keys)
# ===========================================================================
def test_ac13_null_limit_is_unlimited(client: TestClient, monkeypatch):
    from app.rbac import seed_owner

    org = _fresh_org()
    seed_owner(org, "dev-token-change-me")
    ids = [f"sys{i}" for i in range(8)]
    _seed_catalog(ids)
    _set_max_systems(monkeypatch, None)
    hdr = {**AUTH, "X-Org-Id": org}

    for cid in ids:
        assert client.post(f"/api/connectors/{cid}/connect", json={}, headers=hdr).status_code == 200


def test_ac13_missing_limits_block_is_unlimited(monkeypatch):
    """A payload with no limits block at all (older key shape) → unlimited."""
    monkeypatch.setattr(LIMITS_STATUS, lambda *a, **k: {"status": "valid", "payload": {}})
    assert license_limits.get_max_systems("any_org") is None


# ===========================================================================
# R-1.9.1-L1 / T5 (AC4) — Unlicensed connection cap (UNLICENSED_SYSTEM_CAP)
# ===========================================================================
def test_unlicensed_cap_default_is_two(monkeypatch):
    """The config default is 2 (no env override set)."""
    monkeypatch.delenv("UNLICENSED_SYSTEM_CAP", raising=False)
    assert license_limits.get_unlicensed_system_cap() == 2


def test_unlicensed_cap_env_override(monkeypatch):
    """The cap is configurable live via the UNLICENSED_SYSTEM_CAP env var."""
    monkeypatch.setenv("UNLICENSED_SYSTEM_CAP", "5")
    assert license_limits.get_unlicensed_system_cap() == 5
    # ...and get_max_systems reflects it for an unlicensed org.
    _set_no_license(monkeypatch)
    assert license_limits.get_max_systems("any_org") == 5


def test_unlicensed_cap_bad_value_falls_back_to_default(monkeypatch):
    """A non-integer / negative override is ignored (never over-blocks on bad config)."""
    monkeypatch.setenv("UNLICENSED_SYSTEM_CAP", "not-a-number")
    assert license_limits.get_unlicensed_system_cap() == 2
    monkeypatch.setenv("UNLICENSED_SYSTEM_CAP", "-3")
    assert license_limits.get_unlicensed_system_cap() == 2


def test_can_connect_unlicensed_up_to_cap(monkeypatch):
    """AC4 (unit): an unlicensed org can connect up to the cap and no further."""
    _set_no_license(monkeypatch)
    org = _fresh_org()
    _seed_catalog(["a", "b", "c"])
    # 0 connected → can connect a new one.
    assert license_limits.can_connect_new_system(org, "a") is True
    db.org_connector_set(org, "a", {"id": "a", "status": "connected"})
    # 1 connected (< cap 2) → still room.
    assert license_limits.can_connect_new_system(org, "b") is True
    db.org_connector_set(org, "b", {"id": "b", "status": "connected"})
    # 2 connected (== cap) → a brand-new system is refused...
    assert license_limits.can_connect_new_system(org, "c") is False
    # ...but reconnecting an already-connected system is always allowed.
    assert license_limits.can_connect_new_system(org, "a") is True


def test_unlicensed_limit_state_shows_cap(monkeypatch):
    """AC4: the hub state for an unlicensed org shows the cap as systemsLicensed,
    unlimited=False — no longer the pre-T5 'unlimited' reading."""
    _set_no_license(monkeypatch)
    org = _fresh_org()
    _seed_catalog(["a"])
    db.org_connector_set(org, "a", {"id": "a", "status": "connected"})
    state = license_limits.get_limit_state(org)
    # Subset check (not exact-equality): MSP-B13 / T4 (AT-746) added the additive
    # approachingCap/atCap/notice keys to the limit state, which must not break
    # this AC4 core-shape assertion.
    assert state["systemsUsed"] == 1
    assert state["systemsLicensed"] == 2
    assert state["unlimited"] is False
    assert state["canConnectMore"] is True


def test_ac4_unlicensed_connect_up_to_cap_then_blocked(client: TestClient, monkeypatch):
    """AC4 (route): with no license, the first cap connects succeed and the next is
    refused with licensing-specific wording."""
    from app.rbac import seed_owner

    org = _fresh_org()
    seed_owner(org, "dev-token-change-me")
    _seed_catalog(["sys1", "sys2", "sys3"])
    _set_no_license(monkeypatch)  # unlicensed → cap 2
    hdr = {**AUTH, "X-Org-Id": org}

    # First 2 connect normally (within the unlicensed cap).
    assert client.post("/api/connectors/sys1/connect", json={}, headers=hdr).status_code == 200
    assert client.post("/api/connectors/sys2/connect", json={}, headers=hdr).status_code == 200

    # The 3rd is refused — 402, licensing-specific wording that names the license.
    resp = client.post("/api/connectors/sys3/connect", json={}, headers=hdr)
    assert resp.status_code == 402
    detail = resp.json()["detail"]
    assert detail["reason"] == license_limits.BLOCK_REASON
    assert detail["systemsUsed"] == 2
    assert detail["systemsLicensed"] == 2
    assert "license" in detail["detail"].lower()
    assert "No license is installed" in detail["detail"]

    # The block never severed the existing connections (forward-only).
    listed = {c["id"]: c for c in db.org_connectors_list(org)}
    assert listed["sys1"]["status"] == "connected"
    assert listed["sys2"]["status"] == "connected"
    assert listed["sys3"]["status"] != "connected"


def test_ac4_installing_license_lifts_the_cap(client: TestClient, monkeypatch):
    """AC4: installing a valid license lifts the unlicensed cap to the payload's
    max_systems — the (cap+1)th connection that was refused now succeeds."""
    from app.rbac import seed_owner

    org = _fresh_org()
    seed_owner(org, "dev-token-change-me")
    _seed_catalog(["sys1", "sys2", "sys3"])
    hdr = {**AUTH, "X-Org-Id": org}

    _set_no_license(monkeypatch)  # unlicensed → cap 2
    assert client.post("/api/connectors/sys1/connect", json={}, headers=hdr).status_code == 200
    assert client.post("/api/connectors/sys2/connect", json={}, headers=hdr).status_code == 200
    assert client.post("/api/connectors/sys3/connect", json={}, headers=hdr).status_code == 402

    # Install a valid license scoping more systems — the cap lifts, no restart.
    _set_max_systems(monkeypatch, 5)
    assert client.post("/api/connectors/sys3/connect", json={}, headers=hdr).status_code == 200


def test_ac4_valid_license_without_max_systems_is_unlimited(client: TestClient, monkeypatch):
    """AC4: a VALID license with no limits.max_systems stays unlimited — the cap is
    only for the unlicensed case, not for a licensed key that scopes nothing."""
    from app.rbac import seed_owner

    org = _fresh_org()
    seed_owner(org, "dev-token-change-me")
    ids = [f"sys{i}" for i in range(5)]
    _seed_catalog(ids)
    _set_max_systems(monkeypatch, None)  # valid license, no cap
    hdr = {**AUTH, "X-Org-Id": org}

    for cid in ids:  # 5 > the unlicensed cap of 2 — all succeed under the license
        assert client.post(f"/api/connectors/{cid}/connect", json={}, headers=hdr).status_code == 200


# ===========================================================================
# Disconnect / non-connect status changes are never gated by the limit
# ===========================================================================
def test_disconnect_not_blocked_at_limit(client: TestClient, monkeypatch):
    from app.rbac import seed_owner

    org = _fresh_org()
    seed_owner(org, "dev-token-change-me")
    _seed_catalog(["sys1", "sys2"])
    hdr = {**AUTH, "X-Org-Id": org}
    db.org_connector_set(org, "sys1", {"id": "sys1", "status": "connected"})

    _set_max_systems(monkeypatch, 1)  # already at the limit
    # Disconnecting is not a new connection — it must pass through.
    resp = client.post("/api/connectors/sys1/connect", json={"status": "disconnected"}, headers=hdr)
    assert resp.status_code == 200
    assert resp.json()["status"] == "disconnected"
