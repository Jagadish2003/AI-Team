"""2.0-C2 T4 (AT-834) — the certification activation policy over HTTP.

Parent-story criterion exercised here:

  * **AC3** — an org policy restricting to Certified prevents activation of
    Partner/Community packs, with a clear reason.

Plus the properties that make it a control rather than a setting: the Owner-only
write boundary, org isolation, the audit entry (which is this policy's ONLY durable
history), enforcement at the real launch edge, and the fail-closed posture — an
unreadable policy refuses rather than proceeding as if none were set.

The gate itself is pinned DB-free in ``tests/unit/test_pack_certification_policy.py``.
"""
from __future__ import annotations

import os
from typing import Any, Dict, Iterator
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.pack_certification_policy import (
    InMemoryPackCertificationPolicyStore,
    set_policy_store,
)
from app.pack_state import InMemoryPackStateStore, set_pack_state_store
from app.rbac import seed_owner
from discovery.packs import pack_config

OWNER_TOKEN = os.getenv("DEV_JWT", "dev-token-change-me")
ANALYST_TOKEN = "analyst-token"
VIEWER_TOKEN = "viewer-token"

PACK = "cloud_ops"

_CURRENT_ORG: Dict[str, Any] = {"id": None}


@pytest.fixture(autouse=True)
def _role_tokens(monkeypatch):
    monkeypatch.setenv("ANALYST_JWT", ANALYST_TOKEN)
    monkeypatch.setenv("VIEWER_JWT", VIEWER_TOKEN)
    yield


@pytest.fixture(autouse=True)
def _in_memory_stores():
    """Isolate policy and pack state per test, and restore the Postgres stores.

    Critical here: a leaked "Certified only" policy would refuse launches in every
    other suite that shares the contract database.
    """
    set_policy_store(InMemoryPackCertificationPolicyStore())
    set_pack_state_store(InMemoryPackStateStore())
    yield
    set_policy_store(None)
    set_pack_state_store(None)


@pytest.fixture
def isolated_org() -> Iterator[str]:
    previous = _CURRENT_ORG["id"]
    org_id = f"cert_policy_{uuid4().hex[:8]}"
    seed_owner(org_id, OWNER_TOKEN)
    _CURRENT_ORG["id"] = org_id
    try:
        yield org_id
    finally:
        _CURRENT_ORG["id"] = previous


@pytest.fixture
def uncertified_pack(monkeypatch):
    """cloud_ops claims Certified with no signature → effective Community."""
    declaration = dict(pack_config.PACK_REGISTRY[PACK]["certification"])
    declaration["signature"] = {"keyId": "", "algorithm": "", "value": ""}
    monkeypatch.setitem(pack_config.PACK_REGISTRY[PACK], "certification", declaration)
    return PACK


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def _auth(token: str = OWNER_TOKEN, org_id: str | None = None) -> Dict[str, str]:
    headers = {"Authorization": f"Bearer {token}"}
    org = org_id or _CURRENT_ORG["id"]
    if org is not None:
        headers["X-Org-Id"] = org
    return headers


def _launch_body(**overrides: Any) -> Dict[str, Any]:
    """A launch payload matching ``LaunchRequest``.

    Mirrors ``test_pack_disable_lifecycle._launch_body``: ``org_id`` and
    ``weightings`` are required, and the default singular ``pack_id`` is DROPPED when
    the caller supplies ``pack_ids`` — otherwise the selection silently gains
    ``service_cloud``, which would keep a runnable pack in every selection and turn
    the expected 409 into a 200.
    """
    body: Dict[str, Any] = {
        "org_id": "default",
        "selected_system_ids": ["salesforce", "servicenow"],
        "weightings": {},
    }
    if "pack_ids" not in overrides:
        body["pack_id"] = "service_cloud"
    body.update(overrides)
    return body


def _launch(client, **overrides) -> Any:
    return client.post(
        "/api/stack-builder/launch", json=_launch_body(**overrides), headers=_auth()
    )


def _set_policy(client, level: str, **kwargs) -> Any:
    return client.put(
        "/api/packs/certification/policy",
        json={"minimumLevel": level, **kwargs},
        headers=_auth(),
    )


# ── Reading and setting the policy ────────────────────────────────────────────


def test_default_policy_is_unrestricted(client, isolated_org):
    response = client.get("/api/packs/certification/policy", headers=_auth())
    assert response.status_code == 200
    policy = response.json()
    assert policy["minimumLevel"] == "community"
    assert policy["restricted"] is False


def test_owner_sets_a_certified_only_policy(client, isolated_org):
    response = _set_policy(client, "certified", reason="FedRAMP boundary")
    assert response.status_code == 200
    policy = response.json()
    assert policy["minimumLevel"] == "certified"
    assert policy["restricted"] is True
    assert policy["previousMinimumLevel"] == "community"
    assert policy["changed"] is True
    assert policy["reason"] == "FedRAMP boundary"
    assert "Certified" in policy["minimumLevelLabel"]


def test_policy_write_is_idempotent(client, isolated_org):
    _set_policy(client, "certified")
    repeat = _set_policy(client, "certified")
    assert repeat.json()["changed"] is False
    assert repeat.json()["revision"] == 1


def test_lifting_the_restriction_is_recorded_not_deleted(client, isolated_org):
    _set_policy(client, "certified")
    lifted = _set_policy(client, "community", reason="pilot deployment")
    assert lifted.status_code == 200
    assert lifted.json()["restricted"] is False
    assert lifted.json()["previousMinimumLevel"] == "certified"
    assert lifted.json()["revision"] == 2  # a transition, not a disappearance


def test_a_viewer_can_read_the_policy(client):
    """A user who cannot select a pack must be able to see the rule stopping them.

    Runs in the DEFAULT org: the static viewer token holds its role there, whereas a
    freshly seeded throwaway org gives it no role at all — which would make this
    assert RBAC plumbing rather than the read permission under test.
    """
    response = client.get(
        "/api/packs/certification/policy", headers=_auth(VIEWER_TOKEN)
    )
    assert response.status_code == 200, response.text
    assert response.json()["minimumLevel"] in {"community", "partner", "certified"}


def test_an_illegal_level_is_a_bad_request(client, isolated_org):
    response = _set_policy(client, "platinum")
    assert response.status_code == 422  # not in the request model's enum


def test_policy_requires_auth(client, isolated_org):
    assert client.get("/api/packs/certification/policy").status_code in (401, 403)


# ── RBAC and isolation ────────────────────────────────────────────────────────


def test_analyst_cannot_change_the_policy(client):
    response = client.put(
        "/api/packs/certification/policy",
        json={"minimumLevel": "certified"},
        headers=_auth(ANALYST_TOKEN),
    )
    assert response.status_code == 403


def test_viewer_cannot_change_the_policy(client):
    response = client.put(
        "/api/packs/certification/policy",
        json={"minimumLevel": "certified"},
        headers=_auth(VIEWER_TOKEN),
    )
    assert response.status_code == 403


def test_one_orgs_policy_does_not_bind_another(client):
    org_a = f"cert_pol_a_{uuid4().hex[:8]}"
    org_b = f"cert_pol_b_{uuid4().hex[:8]}"
    seed_owner(org_a, OWNER_TOKEN)
    seed_owner(org_b, OWNER_TOKEN)

    client.put(
        "/api/packs/certification/policy",
        json={"minimumLevel": "certified"},
        headers=_auth(org_id=org_a),
    )
    other = client.get(
        "/api/packs/certification/policy", headers=_auth(org_id=org_b)
    )
    assert other.json()["restricted"] is False


# ── Enforcement at activation: AC3 ────────────────────────────────────────────


def test_launch_is_refused_for_a_pack_below_the_floor(
    client, isolated_org, uncertified_pack
):
    """AC3 over the wire, at the real activation edge."""
    _set_policy(client, "certified")
    response = _launch(client, pack_ids=[PACK])
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert PACK in detail                       # names the pack
    assert "Community" in detail                # names the level it holds
    assert "CloudFulcrum Certified" in detail   # names what is required


def test_launch_succeeds_for_a_compliant_pack(client, isolated_org):
    _set_policy(client, "certified")
    response = _launch(client, pack_ids=[PACK])
    assert response.status_code == 200, response.text
    assert PACK in response.json()["packIds"]


def test_launch_is_unaffected_when_no_policy_is_set(
    client, isolated_org, uncertified_pack
):
    """An org that has not opted in is not restricted by anybody else's policy."""
    response = _launch(client, pack_ids=[PACK])
    assert response.status_code == 200, response.text


def test_lifting_the_policy_restores_activation(
    client, isolated_org, uncertified_pack
):
    _set_policy(client, "certified")
    blocked = _launch(client, pack_ids=[PACK])
    assert blocked.status_code == 409, blocked.text

    _set_policy(client, "community", reason="lifted for the pilot")
    allowed = _launch(client, pack_ids=[PACK])
    assert allowed.status_code == 200, allowed.text


# ── Selection surface ─────────────────────────────────────────────────────────


def test_pack_state_reports_the_policy_and_blocked_packs(
    client, isolated_org, uncertified_pack
):
    """A selection surface can grey out a pack rather than 409 after configuration."""
    _set_policy(client, "certified")
    response = client.get("/api/packs/state", headers=_auth())
    assert response.status_code == 200
    body = response.json()
    assert body["certificationPolicy"]["minimumLevel"] == "certified"
    blocked = {row["packId"]: row for row in body["packs"]}
    assert blocked[PACK]["activationBlocked"] is True
    assert blocked[PACK]["activationBlockedReason"]
    assert blocked["ncino"]["activationBlocked"] is False


def test_pack_state_blocks_nothing_without_a_policy(client, isolated_org):
    body = client.get("/api/packs/state", headers=_auth()).json()
    assert body["certificationPolicy"]["restricted"] is False
    assert all(row["activationBlocked"] is False for row in body["packs"])


# ── Auditability ──────────────────────────────────────────────────────────────


def test_the_policy_change_reaches_the_audit_log(client, isolated_org):
    """This policy's table holds current state only — the audit log IS its history."""
    _set_policy(client, "certified", reason="FedRAMP boundary")
    entries = client.get("/api/audit-log", headers=_auth()).json()
    changes = [
        entry
        for entry in entries
        if entry.get("event_type") == "pack_certification_policy_changed"
    ]
    assert changes, "no pack_certification_policy_changed audit entry was written"
    payload = changes[0].get("payload") or {}
    assert payload.get("minimum_level") == "certified"
    assert payload.get("previous_minimum_level") == "community"
    assert changes[0].get("user_id")


def test_a_no_op_policy_write_is_not_an_audit_event(client, isolated_org):
    _set_policy(client, "certified")
    _set_policy(client, "certified")  # no-op
    entries = client.get("/api/audit-log", headers=_auth()).json()
    changes = [
        entry
        for entry in entries
        if entry.get("event_type") == "pack_certification_policy_changed"
    ]
    assert len(changes) == 1
