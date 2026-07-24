"""MSP-B13 / AT-746 (T4) — cloud-connector scope ↔ licence system-count integration.

Each pinned AWS account / Azure subscription is ONE system, counted against the
licence's ``max_systems`` (R17-D4 line) at the moment it is pinned — the pricing
sentence enforced where systems are connected. This suite verifies:

  T4-AC1 — every pinned scope increments the licence system count.
  T4-AC2 — approaching licence capacity surfaces the configured warning.
  T4-AC3 — licence limits prevent additional scope activation (hard stop).
  T4-AC4 — unpinning a scope decrements the system count.

``get_current_license_status`` is monkeypatched (the technique the T9/T10 tests
use) so ``limits.max_systems`` is driven directly without minting a real key. The
provider probes are substituted so no boto3 / network is needed. FAKE CREDENTIALS:
every value below is a non-real, test-only credential.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from cryptography.fernet import Fernet

from app import db, license_limits
import app.routes_cloud_connectors as rcc

_VAULT_KEY = Fernet.generate_key().decode()
AUTH = {"Authorization": "Bearer dev-token-change-me"}
LIMITS_STATUS = "app.license_limits.get_current_license_status"
LIMITS_PATH = "/api/license/limits"

_AWS_KEY = "AKIAFAKEEXAMPLE01234"
_AWS_SECRET = "FAKE/aws/secret/key/abcdefghijklmnopqrstuv"
_AZ_TENANT = "11111111-1111-1111-1111-111111111111"
_AZ_CLIENT = "22222222-2222-2222-2222-222222222222"
_AZ_SECRET = "FAKE-azure-sp-secret-0123456789"
_ROLE_ARN = "arn:aws:iam::{acct}:role/AgentIQReadOnly"


@pytest.fixture(autouse=True)
def _vault_key(monkeypatch):
    monkeypatch.setenv("CREDENTIAL_VAULT_KEY", _VAULT_KEY)
    yield


@pytest.fixture(autouse=True)
def _fake_probes(monkeypatch):
    def _aws_ok(**kwargs):
        return {"identity": "ok"}

    def _aws_assume_ok(org_id, account_config):
        return {"identity": "assumed_role"}

    async def _azure_ok(org_id, *, service_principal, config):
        return {"identity": service_principal.tenant_id}

    monkeypatch.setattr(rcc, "probe_aws_hub_credentials", _aws_ok)
    monkeypatch.setattr(rcc, "probe_aws_assume_role", _aws_assume_ok)
    monkeypatch.setattr(rcc, "probe_azure_service_principal", _azure_ok)
    yield


def _set_max_systems(monkeypatch, value) -> None:
    monkeypatch.setattr(
        LIMITS_STATUS,
        lambda *a, **k: {"status": "valid", "payload": {"limits": {"max_systems": value}}},
    )


def _fresh_org() -> str:
    org = f"org_msp_{uuid.uuid4().hex[:10]}"
    from app.rbac import seed_owner

    seed_owner(org, "dev-token-change-me")
    return org


def _hdr(org: str) -> dict:
    return {**AUTH, "X-Org-Id": org}


def _connect_aws(client, org: str):
    r = client.post(
        "/api/connectors/aws_events",
        headers=_hdr(org),
        json={"partition": "aws", "access_key_id": _AWS_KEY, "secret_access_key": _AWS_SECRET},
    )
    assert r.status_code == 200, r.text


def _pin_aws(client, org: str, account_id: str):
    return client.post(
        "/api/connectors/aws_events/scopes",
        headers=_hdr(org),
        json={"account_id": account_id, "role_arn": _ROLE_ARN.format(acct=account_id)},
    )


def _limits(client, org: str) -> dict:
    resp = client.get(LIMITS_PATH, headers=_hdr(org))
    assert resp.status_code == 200, resp.text
    return resp.json()


# ===========================================================================
# Unit — count_connected_systems is scope-aware (AC1)
# ===========================================================================
def test_count_treats_pinned_scopes_as_systems():
    org = _fresh_org()
    db.upsert("connectors", "sys1", {"id": "sys1", "name": "Sys1", "status": "disconnected"})
    # A single-scope connector counts as one when connected.
    db.org_connector_set(org, "sys1", {"id": "sys1", "status": "connected"})
    # A multi-scope connector counts its PINNED scopes, not the connection itself.
    db.org_connector_set(org, "aws_events", {
        "id": "aws_events", "multiScope": True, "status": "connected",
        "scopes": [{"scope_id": "a"}, {"scope_id": "b"}],
        "candidate_scopes": ["c"],  # candidates never count (forward-only)
    })
    assert license_limits.count_connected_systems(org) == 3  # 1 + 2 pinned


def test_multi_scope_connection_with_no_scopes_counts_zero():
    """Creating the connection is not itself a billable system — only pinned scopes."""
    org = _fresh_org()
    db.org_connector_set(org, "azure_events", {
        "id": "azure_events", "multiScope": True, "status": "connected", "scopes": [],
    })
    assert license_limits.count_connected_systems(org) == 0


# ===========================================================================
# T4-AC1 — every pinned scope increments the licence system count
# ===========================================================================
def test_ac1_each_pinned_scope_increments_count(client, monkeypatch):
    org = _fresh_org()
    _set_max_systems(monkeypatch, 10)
    _connect_aws(client, org)

    assert _limits(client, org)["systemsUsed"] == 0     # connection alone = 0 systems
    assert _pin_aws(client, org, "100000000001").status_code == 200
    assert _limits(client, org)["systemsUsed"] == 1
    assert _pin_aws(client, org, "100000000002").status_code == 200
    assert _limits(client, org)["systemsUsed"] == 2


def test_ac1_azure_and_aws_scopes_both_count(client, monkeypatch):
    org = _fresh_org()
    _set_max_systems(monkeypatch, 10)
    _connect_aws(client, org)
    client.post(
        "/api/connectors/azure_events",
        headers=_hdr(org),
        json={"environment": "AzureCloud", "mode": "lighthouse",
              "tenant_id": _AZ_TENANT, "client_id": _AZ_CLIENT, "client_secret": _AZ_SECRET},
    )
    _pin_aws(client, org, "100000000001")
    client.post(
        "/api/connectors/azure_events/scopes",
        headers=_hdr(org), json={"subscription_id": "sub-1"},
    )
    assert _limits(client, org)["systemsUsed"] == 2


# ===========================================================================
# T4-AC2 — approaching capacity surfaces the configured warning
# ===========================================================================
def test_ac2_approaching_cap_notice(client, monkeypatch):
    org = _fresh_org()
    _set_max_systems(monkeypatch, 2)      # margin default 1 → warn at the last seat
    _connect_aws(client, org)
    _pin_aws(client, org, "100000000001")   # used 1 of 2 → 1 remaining

    state = _limits(client, org)
    assert state["systemsUsed"] == 1
    assert state["approachingCap"] is True
    assert state["atCap"] is False
    assert "approaching your licence limit" in state["notice"]


def test_ac2_not_approaching_when_comfortably_under(client, monkeypatch):
    org = _fresh_org()
    _set_max_systems(monkeypatch, 5)
    _connect_aws(client, org)
    _pin_aws(client, org, "100000000001")   # used 1 of 5 → 4 remaining

    state = _limits(client, org)
    assert state["approachingCap"] is False
    assert state["notice"] is None


def test_ac2_margin_is_configurable(client, monkeypatch):
    monkeypatch.setenv("LICENSE_APPROACHING_CAP_MARGIN", "3")
    org = _fresh_org()
    _set_max_systems(monkeypatch, 5)
    _connect_aws(client, org)
    _pin_aws(client, org, "100000000001")   # used 1, remaining 4 → not within 3
    assert _limits(client, org)["approachingCap"] is False
    _pin_aws(client, org, "100000000002")   # used 2, remaining 3 → within 3
    assert _limits(client, org)["approachingCap"] is True


# ===========================================================================
# T4-AC3 — licence limits prevent additional scope activation
# ===========================================================================
def test_ac3_hard_stop_at_cap(client, monkeypatch):
    org = _fresh_org()
    _set_max_systems(monkeypatch, 2)
    _connect_aws(client, org)
    assert _pin_aws(client, org, "100000000001").status_code == 200
    assert _pin_aws(client, org, "100000000002").status_code == 200

    state = _limits(client, org)
    assert state["atCap"] is True
    assert state["canConnectMore"] is False

    # The (N+1)th pin is blocked with 402 and the hard-stop wording + counts.
    blocked = _pin_aws(client, org, "100000000003")
    assert blocked.status_code == 402, blocked.text
    detail = blocked.json()["detail"]
    assert detail["reason"] == "system_limit_reached"
    assert detail["systemsUsed"] == 2
    assert detail["systemsLicensed"] == 2
    assert "Contact CloudFulcrum" in detail["detail"]
    # And the blocked scope was NOT pinned.
    scopes = client.get("/api/connectors/aws_events/scopes", headers=_hdr(org)).json()["scopes"]
    assert "100000000003" not in [s["scope_id"] for s in scopes]


def test_ac3_repin_existing_scope_at_cap_is_allowed(client, monkeypatch):
    """Re-pinning an already-pinned scope is idempotent — never blocked (forward-only)."""
    org = _fresh_org()
    _set_max_systems(monkeypatch, 1)
    _connect_aws(client, org)
    assert _pin_aws(client, org, "100000000001").status_code == 200   # used 1 of 1
    # Re-pin the SAME account at the cap → still allowed, count unchanged.
    assert _pin_aws(client, org, "100000000001").status_code == 200
    assert _limits(client, org)["systemsUsed"] == 1


def test_ac3_unlimited_licence_never_blocks(client, monkeypatch):
    org = _fresh_org()
    _set_max_systems(monkeypatch, None)
    _connect_aws(client, org)
    for i in range(4):
        assert _pin_aws(client, org, f"10000000000{i}").status_code == 200
    state = _limits(client, org)
    assert state["unlimited"] is True
    assert state["systemsUsed"] == 4


# ===========================================================================
# T4-AC4 — unpinning a scope decrements the system count
# ===========================================================================
def test_ac4_unpin_decrements_count(client, monkeypatch):
    org = _fresh_org()
    _set_max_systems(monkeypatch, 5)
    _connect_aws(client, org)
    _pin_aws(client, org, "100000000001")
    _pin_aws(client, org, "100000000002")
    assert _limits(client, org)["systemsUsed"] == 2

    r = client.delete("/api/connectors/aws_events/scopes/100000000001", headers=_hdr(org))
    assert r.status_code == 204
    assert _limits(client, org)["systemsUsed"] == 1


def test_ac4_unpin_frees_a_seat_at_cap(client, monkeypatch):
    """After unpinning at the cap, a new scope can be pinned again (AC4 + AC3)."""
    org = _fresh_org()
    _set_max_systems(monkeypatch, 1)
    _connect_aws(client, org)
    assert _pin_aws(client, org, "100000000001").status_code == 200
    # At the cap — a new pin is blocked.
    assert _pin_aws(client, org, "100000000002").status_code == 402
    # Unpin frees the seat.
    client.delete("/api/connectors/aws_events/scopes/100000000001", headers=_hdr(org))
    assert _limits(client, org)["systemsUsed"] == 0
    # Now a new scope pins successfully.
    assert _pin_aws(client, org, "100000000002").status_code == 200
    assert _limits(client, org)["systemsUsed"] == 1
