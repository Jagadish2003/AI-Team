"""Contract tests — R17-D4 Addendum A / T14 (AT-509): full-addendum acceptance suite.

End-to-end coverage of Addendum A Section 4 (AC10–AC16) driven through the REAL
license key lifecycle. A signed key is minted carrying ``limits.max_systems`` and
``org_name`` and installed via ``POST /api/license/update-key``; scoped-activation
enforcement (the connect gate + the limit-state endpoint) and the dynamic org
name (the org-name endpoint) are then observed against that installed key — and
against a *replacement* key, proving the no-restart renewal path (AC11/AC15).

Why this complements the per-task suites: ``test_license_max_systems`` (T9),
``test_license_limits_state`` (T10) and ``test_license_org_name`` (T12) each stub
``get_current_license_status`` to drive the org's validated payload directly. That
proves each unit's logic, but not that a *real pasted key's* fields actually reach
the gate and the endpoints. Here nothing is stubbed on the license-read path: the
key is really Ed25519-verified (via the ``LICENSE_PUBLIC_KEY`` override, as
``test_license_routes`` does), really stored per-org, and really re-validated live
on each request — so these tests prove §1 (scope) and §2 (name) are wired to the
one signed source of truth (Addendum A §5), together, through the actual install
path.

The frontend halves of AC15/AC16 (the name propagating to every UI surface with
no restart / no stale naming pre-activation) are covered by the T13 Vitest suites
(``LicenseContextOrgName``, ``TopNavOrgName``, ``LicensePage``,
``ExecutiveReportPage``); these backend contract tests cover the API behaviour
those surfaces consume.
"""
from __future__ import annotations

import datetime
import uuid

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from app import db, license_limits
from app.org_display_name import DEFAULT_ORG_DISPLAY_NAME
from license.generate_license import DEFAULT_KID, build_payload, sign_payload

AUTH = {"Authorization": "Bearer dev-token-change-me"}
DEV_USER = "dev-token-change-me"

UPDATE_PATH = "/api/license/update-key"
LIMITS_PATH = "/api/license/limits"
ORG_NAME_PATH = "/api/license/org-name"


# ---------------------------------------------------------------------------
# Helpers — real keys through the real install path (no license-status stubbing)
# ---------------------------------------------------------------------------
def _pub_pem(priv: Ed25519PrivateKey) -> str:
    return priv.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()


def _trust(monkeypatch) -> Ed25519PrivateKey:
    """Mint a throwaway signer and make the app trust its public key, so keys we
    sign here validate for real (Ed25519) without the CloudFulcrum private key."""
    priv = Ed25519PrivateKey.generate()
    monkeypatch.setenv("LICENSE_PUBLIC_KEY", _pub_pem(priv))
    return priv


def _mint(
    priv: Ed25519PrivateKey,
    *,
    org=None,
    max_systems=None,
    org_name=None,
    customer="City National Bank",
    term_months: int = 12,
) -> str:
    """A real signed key with the addendum fields baked in. ``term_months=12``
    means ``expires_at`` is ~360 days out, so the installed key is ``valid``.

    ``org`` binds the payload's ``org_id`` to the installation org so the key
    clears org binding (T2) as well as the T4 version gate — ``build_payload``
    already stamps ``payload_version=2`` + a default ``kid``, so the payload is
    v2-shaped."""
    payload = build_payload(
        customer,
        f"lic-{uuid.uuid4().hex[:8]}",
        term_months,
        14,
        max_systems=max_systems,
        org_name=org_name,
        org_id=org,
    )
    return sign_payload(payload, priv)


def _mint_raw(priv: Ed25519PrivateKey, payload: dict) -> str:
    """Sign an explicit payload — used to simulate a PRE-addendum key that omits
    ``org_name`` / ``limits`` entirely (a key issued before the fields existed)."""
    return sign_payload(payload, priv)


def _future(days: int = 300) -> str:
    return (datetime.date.today() + datetime.timedelta(days=days)).isoformat()


def _owner_org() -> str:
    """A fresh org with the dev user as owner (update-key + connect are owner-gated)."""
    from app.rbac import seed_owner

    org = f"org_addA_{uuid.uuid4().hex[:10]}"
    seed_owner(org, DEV_USER)
    return org


def _seed_catalog(connector_ids) -> None:
    """Seed shared (org-less) catalog rows so the connect route finds the ids."""
    for cid in connector_ids:
        db.upsert(
            "connectors",
            cid,
            {"id": cid, "name": cid.title(), "status": "disconnected"},
        )


def _install(client: TestClient, org: str, key: str) -> dict:
    """Paste a key via the real Owner-only update-key route; assert it stored."""
    resp = client.post(UPDATE_PATH, json={"key": key}, headers={**AUTH, "X-Org-Id": org})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _connect(client: TestClient, org: str, cid: str, status: str = "connected"):
    return client.post(
        f"/api/connectors/{cid}/connect",
        json={"status": status} if status != "connected" else {},
        headers={**AUTH, "X-Org-Id": org},
    )


# ===========================================================================
# AC10 — with max_systems = N, the first N connect; the (N+1)th is blocked
#         with a clear message + request path. (Real installed key.)
# ===========================================================================
def test_ac10_installed_key_blocks_nth_plus_one(client: TestClient, monkeypatch):
    priv = _trust(monkeypatch)
    org = _owner_org()
    _seed_catalog(["sys1", "sys2", "sys3"])
    _install(client, org, _mint(priv, org=org, max_systems=2, org_name="Teachers Credit Union"))

    # First 2 connect normally (within the licensed scope).
    assert _connect(client, org, "sys1").status_code == 200
    assert _connect(client, org, "sys2").status_code == 200

    # The 3rd (N+1) is blocked with the clear message + request path.
    blocked = _connect(client, org, "sys3")
    assert blocked.status_code == 402
    detail = blocked.json()["detail"]
    assert detail["reason"] == license_limits.BLOCK_REASON
    assert detail["systemsUsed"] == 2
    assert detail["systemsLicensed"] == 2
    assert "Contact CloudFulcrum" in detail["detail"]
    assert "2 systems" in detail["detail"]

    # Forward-only: the block never severed the existing connections.
    listed = {c["id"]: c for c in db.org_connectors_list(org)}
    assert listed["sys1"]["status"] == "connected"
    assert listed["sys2"]["status"] == "connected"
    assert listed["sys3"]["status"] != "connected"


# ===========================================================================
# AC11 — installing a key with a HIGHER limit immediately allows more
#         connections, with no restart. (Real key swap via update-key.)
# ===========================================================================
def test_ac11_higher_limit_key_unblocks_no_restart(client: TestClient, monkeypatch):
    priv = _trust(monkeypatch)
    org = _owner_org()
    _seed_catalog(["sys1", "sys2", "sys3"])
    _install(client, org, _mint(priv, org=org, max_systems=2))

    assert _connect(client, org, "sys1").status_code == 200
    assert _connect(client, org, "sys2").status_code == 200
    assert _connect(client, org, "sys3").status_code == 402  # at the cap

    # Paste a renewed key with a higher limit — the existing LIC-1 renewal path.
    _install(client, org, _mint(priv, org=org, max_systems=3))

    # No restart — the very next attempt (same running client) succeeds.
    assert _connect(client, org, "sys3").status_code == 200


# ===========================================================================
# AC12 — a new key with a LOWER limit than currently-connected systems never
#         disconnects existing systems; it only blocks NEW ones.
# ===========================================================================
def test_ac12_lower_limit_key_never_disconnects(client: TestClient, monkeypatch):
    priv = _trust(monkeypatch)
    org = _owner_org()
    _seed_catalog(["sys1", "sys2", "sys3", "sys4"])
    # Connect three systems under a generous key.
    _install(client, org, _mint(priv, org=org, max_systems=5))
    for cid in ("sys1", "sys2", "sys3"):
        assert _connect(client, org, cid).status_code == 200

    # A renewed key arrives carrying a LOWER limit (2) than the 3 connected.
    _install(client, org, _mint(priv, org=org, max_systems=2))

    # Existing connections are untouched — never auto-disconnected (never a cold stop).
    listed = {c["id"]: c for c in db.org_connectors_list(org)}
    assert listed["sys1"]["status"] == "connected"
    assert listed["sys2"]["status"] == "connected"
    assert listed["sys3"]["status"] == "connected"

    # A brand-new system is blocked (already over the reduced limit).
    blocked = _connect(client, org, "sys4")
    assert blocked.status_code == 402
    assert blocked.json()["detail"]["reason"] == license_limits.BLOCK_REASON

    # Re-connecting an already-connected system still works (forward-only, idempotent).
    assert _connect(client, org, "sys1").status_code == 200


# ===========================================================================
# AC13 — max_systems null behaves as unlimited (backwards compatible).
# ===========================================================================
def test_ac13_null_limit_key_is_unlimited(client: TestClient, monkeypatch):
    priv = _trust(monkeypatch)
    org = _owner_org()
    ids = [f"sys{i}" for i in range(8)]
    _seed_catalog(ids)
    _install(client, org, _mint(priv, org=org, max_systems=None, org_name="Unlimited Co"))

    for cid in ids:
        assert _connect(client, org, cid).status_code == 200

    state = client.get(LIMITS_PATH, headers={**AUTH, "X-Org-Id": org}).json()
    assert state["systemsLicensed"] is None
    assert state["unlimited"] is True
    assert state["canConnectMore"] is True
    assert state["systemsUsed"] == len(ids)


def test_ac13_pre_addendum_key_without_limits_is_unlimited(client: TestClient, monkeypatch):
    """A key that omits the limits block entirely (as a pre-addendum key did) must
    still validate and be treated as unlimited (opt-in enforcement per key). The
    payload is v2-shaped so it clears the T4 gate; only the addendum fields are
    absent."""
    priv = _trust(monkeypatch)
    org = _owner_org()
    _seed_catalog(["sys1", "sys2", "sys3"])
    # A v2 key that omits the addendum fields (no limits, no org_name). It is
    # still v2-shaped (org_id + kid), so it clears the T4 version gate; the point
    # is that the OPTIONAL addendum fields being absent → unlimited.
    payload = {
        "customer": "Legacy Bank",
        "license_id": "legacy-001",
        "issued_at": "2026-01-01",
        "expires_at": _future(),
        "term_months": 12,
        "grace_days": 14,
        "org_id": org,
        "kid": DEFAULT_KID,
    }
    _install(client, org, _mint_raw(priv, payload))

    for cid in ("sys1", "sys2", "sys3"):
        assert _connect(client, org, cid).status_code == 200
    assert client.get(LIMITS_PATH, headers={**AUTH, "X-Org-Id": org}).json()["unlimited"] is True


# ===========================================================================
# AC14 — the Integration Hub shows systems-used vs systems-licensed, and the
#         count it shows is exactly the count that is enforced.
# ===========================================================================
def test_ac14_shown_count_matches_enforced_count(client: TestClient, monkeypatch):
    priv = _trust(monkeypatch)
    org = _owner_org()
    _seed_catalog(["sys1", "sys2", "sys3"])
    _install(client, org, _mint(priv, org=org, max_systems=2))
    hdr = {**AUTH, "X-Org-Id": org}

    assert _connect(client, org, "sys1").status_code == 200
    assert _connect(client, org, "sys2").status_code == 200

    # The hub-facing state shows we are exactly at the cap, no headroom. Subset
    # check (not exact-equality): MSP-B13 / T4 added additive keys
    # (approachingCap/atCap/notice) to this response, which must not break the
    # T10 core-shape assertions.
    state = client.get(LIMITS_PATH, headers=hdr).json()
    assert state["systemsUsed"] == 2
    assert state["systemsLicensed"] == 2
    assert state["unlimited"] is False
    assert state["canConnectMore"] is False

    # The enforced block on the (N+1)th connect carries the SAME numbers — one
    # system = one connected entity, shown == enforced.
    blocked = _connect(client, org, "sys3")
    assert blocked.status_code == 402
    detail = blocked.json()["detail"]
    assert detail["systemsUsed"] == state["systemsUsed"]
    assert detail["systemsLicensed"] == state["systemsLicensed"]


def test_ac14_state_reflects_higher_limit_key_no_restart(client: TestClient, monkeypatch):
    """AC11 seen from the hub state: a higher-limit key flips canConnectMore back
    to True immediately, no restart."""
    priv = _trust(monkeypatch)
    org = _owner_org()
    _seed_catalog(["sys1"])
    hdr = {**AUTH, "X-Org-Id": org}

    _install(client, org, _mint(priv, org=org, max_systems=1))
    assert _connect(client, org, "sys1").status_code == 200
    at_limit = client.get(LIMITS_PATH, headers=hdr).json()
    assert at_limit["systemsUsed"] == 1
    assert at_limit["systemsLicensed"] == 1
    assert at_limit["unlimited"] is False
    assert at_limit["canConnectMore"] is False

    _install(client, org, _mint(priv, org=org, max_systems=3))
    after = client.get(LIMITS_PATH, headers=hdr).json()
    assert after["systemsLicensed"] == 3
    assert after["canConnectMore"] is True


# ===========================================================================
# AC15 — after a key is installed the org_name from the payload is served, and
#         pasting a key with a different org_name updates it with no restart.
# ===========================================================================
def test_ac15_org_name_from_installed_key(client: TestClient, monkeypatch):
    priv = _trust(monkeypatch)
    org = _owner_org()
    _install(client, org, _mint(priv, org=org, org_name="Teachers Credit Union"))

    resp = client.get(ORG_NAME_PATH, headers={**AUTH, "X-Org-Id": org})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"orgName": "Teachers Credit Union"}


def test_ac15_org_name_updates_on_new_key_no_restart(client: TestClient, monkeypatch):
    priv = _trust(monkeypatch)
    org = _owner_org()
    hdr = {**AUTH, "X-Org-Id": org}

    _install(client, org, _mint(priv, org=org, org_name="Teachers Credit Union"))
    assert client.get(ORG_NAME_PATH, headers=hdr).json() == {"orgName": "Teachers Credit Union"}

    # Paste a key with a different org_name — same running client, no restart.
    _install(client, org, _mint(priv, org=org, org_name="Teachers Credit Union of Indiana"))
    assert client.get(ORG_NAME_PATH, headers=hdr).json() == {
        "orgName": "Teachers Credit Union of Indiana"
    }


def test_ac15_pre_addendum_key_falls_back_to_customer(client: TestClient, monkeypatch):
    """A key without org_name shows the real customer name (correct, not stale)."""
    priv = _trust(monkeypatch)
    org = _owner_org()
    payload = {
        "customer": "City National Bank",
        "license_id": "legacy-002",
        "issued_at": "2026-01-01",
        "expires_at": _future(),
        "term_months": 12,
        "grace_days": 14,
        # v2-shaped but with no org_name → the display name falls back to customer.
        "org_id": org,
        "kid": DEFAULT_KID,
    }
    _install(client, org, _mint_raw(priv, payload))
    assert client.get(ORG_NAME_PATH, headers={**AUTH, "X-Org-Id": org}).json() == {
        "orgName": "City National Bank"
    }


# ===========================================================================
# AC16 — before any key is installed, a neutral default is shown — no stale or
#         placeholder customer naming anywhere.
# ===========================================================================
def test_ac16_neutral_default_before_any_key(client: TestClient):
    org = _owner_org()  # fresh org, NO key installed
    hdr = {**AUTH, "X-Org-Id": org}

    # Org name is the neutral default — never a customer/placeholder identity.
    assert client.get(ORG_NAME_PATH, headers=hdr).json() == {"orgName": DEFAULT_ORG_DISPLAY_NAME}
    assert DEFAULT_ORG_DISPLAY_NAME == "Your Organisation"

    # An unlicensed org is now capped at the unlicensed cap (default 2), not
    # unlimited (R-1.9.1-L1 / T5, AC4) — a real numeric limit before any key.
    state = client.get(LIMITS_PATH, headers=hdr).json()
    assert state["systemsLicensed"] == 2
    assert state["unlimited"] is False


# ===========================================================================
# §5 "One name, resolved once" — a single installed key drives BOTH the scope
# limit (§1) and the display name (§2). Ties the whole addendum to one source.
# ===========================================================================
def test_single_key_drives_both_scope_and_name(client: TestClient, monkeypatch):
    priv = _trust(monkeypatch)
    org = _owner_org()
    _seed_catalog(["sys1", "sys2"])
    hdr = {**AUTH, "X-Org-Id": org}

    _install(client, org, _mint(priv, org=org, max_systems=1, org_name="Teachers Credit Union"))

    # §2 — the name from the same key.
    assert client.get(ORG_NAME_PATH, headers=hdr).json() == {"orgName": "Teachers Credit Union"}

    # §1 — the scope from the same key: one system connects, the next is blocked.
    assert _connect(client, org, "sys1").status_code == 200
    assert _connect(client, org, "sys2").status_code == 402
    limits_state = client.get(LIMITS_PATH, headers=hdr).json()
    assert limits_state["systemsUsed"] == 1
    assert limits_state["systemsLicensed"] == 1
    assert limits_state["unlimited"] is False
    assert limits_state["canConnectMore"] is False


@pytest.mark.parametrize("role", ["owner", "analyst", "viewer"])
def test_org_name_and_limits_readable_by_every_role(client: TestClient, monkeypatch, role):
    """Both hub-facing reads are auth-only (viewer+), so every role that sees the
    header / Integration Hub sees the same resolved name and usage (AC14/AC15)."""
    from datetime import datetime, timezone

    from app.rbac import _ensure_members_table

    _ensure_members_table()
    org = f"org_addA_role_{uuid.uuid4().hex[:8]}"
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
    hdr = {**AUTH, "X-Org-Id": org}

    assert client.get(ORG_NAME_PATH, headers=hdr).status_code == 200
    assert client.get(LIMITS_PATH, headers=hdr).status_code == 200
