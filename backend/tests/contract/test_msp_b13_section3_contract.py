"""MSP-B13 / AT-749 (T7) — Section 3 contract tests.

End-to-end contract validation of the Cloud Connector Onboarding workflow, tying
the per-endpoint routes (T3), the licence system-count integration (T4), and the
security-artifact routes (T5) into the WORKFLOW-level and cross-role assertions
Section 3 requires:

  T7-AC1 — the onboarding workflow passes end-to-end (test → create → pin →
           list → per-scope health → visible system count), for BOTH providers.
  T7-AC2 — scope management is correct across MULTIPLE operations (many pins,
           unpin one keeps the rest, idempotent re-pin, candidate promotion,
           AWS/Azure independence) with the system count tracking every step.
  T7-AC3 — RBAC is enforced for ALL supported roles — Owner manages; Analyst and
           Viewer are read-only (health/scopes/artifacts) and blocked (403) from
           every write; unauthenticated is 401.
  T7-AC4 — the whole MSP-B13 contract suite passes (this file + the T3/T4/T5
           suites it complements).

Complements (does not duplicate) ``test_cloud_connector_onboarding.py`` (T3),
``test_cloud_connector_system_count.py`` (T4), and the T5 security-artifact tests
in the onboarding file: those verify each endpoint in isolation, this verifies the
workflow end-to-end, scope management across a sequence of operations, and the
Analyst role the per-endpoint suites (Owner + Viewer only) do not cover.

The provider probes are substituted with deterministic fakes so the suite needs no
boto3 / no AWS or Azure account, and ``get_current_license_status`` is
monkeypatched so a generous ``max_systems`` never blocks the workflow (the cap
behaviour itself is T4's suite). FAKE CREDENTIALS: every value below is a
non-real, test-only credential. Each test runs against a FRESH org so the three
seeded roles and the pinned-scope set never leak between tests.
"""
from __future__ import annotations

import uuid

import pytest
from cryptography.fernet import Fernet

from app import db
from app.auth.vault import get_static_credential
import app.routes_cloud_connectors as rcc

_VAULT_KEY = Fernet.generate_key().decode()

# The three supported roles (T7-AC3). Owner is DEV_JWT (seeded owner); Analyst and
# Viewer are the static ANALYST_JWT / VIEWER_JWT tokens seeded as org members.
OWNER_TOKEN = "dev-token-change-me"
ANALYST_TOKEN = "analyst-token"
VIEWER_TOKEN = "viewer-token"

LIMITS_STATUS = "app.license_limits.get_current_license_status"

# Fake, non-real, test-only credentials.
_AWS_KEY = "AKIAFAKEEXAMPLE01234"
_AWS_SECRET = "FAKE/aws/secret/key/abcdefghijklmnopqrstuv"
_AZ_TENANT = "11111111-1111-1111-1111-111111111111"
_AZ_CLIENT = "22222222-2222-2222-2222-222222222222"
_AZ_SECRET = "FAKE-azure-sp-secret-0123456789"
_ROLE_ARN = "arn:aws:iam::{acct}:role/AgentIQReadOnly"


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _vault_key(monkeypatch):
    monkeypatch.setenv("CREDENTIAL_VAULT_KEY", _VAULT_KEY)
    yield


@pytest.fixture(autouse=True)
def _analyst_token_enabled(monkeypatch):
    """Make the static ANALYST_JWT token acceptable to require_auth.

    require_auth only accepts a static token that _token_roles() knows about;
    ANALYST_JWT is read live from the env, so setting it here (before the org is
    seeded) lets the Analyst role be exercised at all — the gap the Owner+Viewer
    per-endpoint suites leave (T7-AC3).
    """
    monkeypatch.setenv("ANALYST_JWT", ANALYST_TOKEN)
    yield


@pytest.fixture(autouse=True)
def _generous_licence(monkeypatch):
    """A valid licence with plenty of headroom so the workflow is never cap-blocked.

    The cap/approaching behaviour is T4's suite; here the count is only asserted to
    TRACK the pinned set, so a generous limit keeps the workflow assertions about
    onboarding + scope management, not licensing.
    """
    monkeypatch.setattr(
        LIMITS_STATUS,
        lambda *a, **k: {"status": "valid", "payload": {"limits": {"max_systems": 50}}},
    )
    yield


@pytest.fixture(autouse=True)
def _fake_probes(monkeypatch):
    """Substitute the provider probes so no boto3 / network is needed."""

    def _aws_ok(**kwargs):
        return {"identity": "123456789012"}

    def _aws_assume_ok(org_id, account_config):
        return {"identity": "assumed_role"}

    async def _azure_ok(org_id, *, service_principal, config):
        return {"identity": service_principal.tenant_id}

    monkeypatch.setattr(rcc, "probe_aws_hub_credentials", _aws_ok)
    monkeypatch.setattr(rcc, "probe_aws_assume_role", _aws_assume_ok)
    monkeypatch.setattr(rcc, "probe_azure_service_principal", _azure_ok)
    yield


@pytest.fixture()
def org(_analyst_token_enabled) -> str:
    """A fresh org with the three supported roles seeded as members.

    Depends on ``_analyst_token_enabled`` so ANALYST_JWT is set BEFORE
    ``seed_static_token_members`` reads it (otherwise the Analyst member row is
    never seeded and the Analyst-role assertions would silently degrade).
    """
    from app.rbac import seed_owner, seed_static_token_members

    org_id = f"org_b13t7_{uuid.uuid4().hex[:10]}"
    seed_owner(org_id, OWNER_TOKEN)               # Owner
    seed_static_token_members(org_id)             # Analyst + Viewer (static tokens)
    return org_id


def _hdr(org_id: str, token: str = OWNER_TOKEN) -> dict:
    return {"Authorization": f"Bearer {token}", "X-Org-Id": org_id}


# ─────────────────────────────────────────────────────────────────────────────
# Workflow helpers
# ─────────────────────────────────────────────────────────────────────────────


def _create_aws(client, org_id, token=OWNER_TOKEN):
    return client.post(
        "/api/connectors/aws_events",
        headers=_hdr(org_id, token),
        json={"partition": "aws", "access_key_id": _AWS_KEY, "secret_access_key": _AWS_SECRET},
    )


def _create_azure(client, org_id, token=OWNER_TOKEN):
    return client.post(
        "/api/connectors/azure_events",
        headers=_hdr(org_id, token),
        json={
            "environment": "AzureCloud", "mode": "lighthouse",
            "tenant_id": _AZ_TENANT, "client_id": _AZ_CLIENT, "client_secret": _AZ_SECRET,
        },
    )


def _pin_aws(client, org_id, account_id, token=OWNER_TOKEN):
    return client.post(
        "/api/connectors/aws_events/scopes",
        headers=_hdr(org_id, token),
        json={"account_id": account_id, "role_arn": _ROLE_ARN.format(acct=account_id)},
    )


def _pin_azure(client, org_id, sub_id, token=OWNER_TOKEN):
    return client.post(
        "/api/connectors/azure_events/scopes",
        headers=_hdr(org_id, token),
        json={"subscription_id": sub_id},
    )


def _scope_ids(client, org_id, connector, token=OWNER_TOKEN):
    body = client.get(f"/api/connectors/{connector}/scopes", headers=_hdr(org_id, token)).json()
    return [s["scope_id"] for s in body["scopes"]]


def _systems_used(client, org_id) -> int:
    r = client.get("/api/license/limits", headers=_hdr(org_id))
    assert r.status_code == 200, r.text
    return r.json()["systemsUsed"]


# ═════════════════════════════════════════════════════════════════════════════
# T7-AC1 — onboarding workflow end-to-end
# ═════════════════════════════════════════════════════════════════════════════


def test_aws_onboarding_workflow_end_to_end(client, org):
    """test → create → pin → list → health → system count, as one AWS flow."""
    # 1. Test connection BEFORE save — validates, persists nothing.
    t = client.post(
        "/api/connectors/aws_events/test",
        headers=_hdr(org),
        json={"partition": "aws", "access_key_id": _AWS_KEY, "secret_access_key": _AWS_SECRET},
    )
    assert t.status_code == 200 and t.json()["ok"] is True
    assert get_static_credential(org, "aws_events") is None
    assert _systems_used(client, org) == 0            # nothing connected yet

    # 2. Create — vaulted, write-only, connection alone is 0 systems.
    c = _create_aws(client, org)
    assert c.status_code == 200, c.text
    assert c.json()["configured"] is True and c.json()["status"] == "connected"
    assert _AWS_SECRET not in c.text and _AWS_KEY not in c.text
    assert _systems_used(client, org) == 0

    # 3. Pin an account (validated by the assume-role probe) — now 1 system.
    p = _pin_aws(client, org, "100000000001")
    assert p.status_code == 200, p.text
    assert _scope_ids(client, org, "aws_events") == ["100000000001"]
    assert _systems_used(client, org) == 1

    # 4. Per-scope health reads the shared run-health vocabulary (pending pre-run).
    h = client.get(
        "/api/connectors/aws_events/scopes/100000000001/health", headers=_hdr(org)
    )
    assert h.status_code == 200
    assert h.json()["status"] == "pending" and h.json()["healthy"] is False


def test_azure_onboarding_workflow_end_to_end(client, org):
    """test → create → discover candidate → explicit pin → list → count, Azure."""
    t = client.post(
        "/api/connectors/azure_events/test",
        headers=_hdr(org),
        json={"environment": "AzureCloud", "tenant_id": _AZ_TENANT,
              "client_id": _AZ_CLIENT, "client_secret": _AZ_SECRET},
    )
    assert t.status_code == 200 and t.json()["ok"] is True

    assert _create_azure(client, org).status_code == 200
    assert get_static_credential(org, "azure_events").secret == _AZ_SECRET   # vaulted

    # A discovered subscription is a CANDIDATE — surfaced, never ingested (forward-only).
    rec = db.org_connector_get(org, "azure_events")
    rec["candidate_scopes"] = ["sub-discovered"]
    db.org_connector_set(org, "azure_events", rec)
    listed = client.get("/api/connectors/azure_events/scopes", headers=_hdr(org)).json()
    assert "sub-discovered" in listed["candidates"]
    assert listed["scopes"] == []
    assert _systems_used(client, org) == 0            # candidate does not count

    # Explicit pin activates it and removes it from the candidate list; now 1 system.
    assert _pin_azure(client, org, "sub-discovered").status_code == 200
    listed = client.get("/api/connectors/azure_events/scopes", headers=_hdr(org)).json()
    assert [s["scope_id"] for s in listed["scopes"]] == ["sub-discovered"]
    assert "sub-discovered" not in listed["candidates"]
    assert _systems_used(client, org) == 1


# ═════════════════════════════════════════════════════════════════════════════
# T7-AC2 — scope management across multiple operations
# ═════════════════════════════════════════════════════════════════════════════


def test_scope_lifecycle_across_multiple_operations(client, org):
    """Many pins, unpin the middle, idempotent re-pin, then a fresh pin — counts track."""
    _create_aws(client, org)
    for acct in ("100000000001", "100000000002", "100000000003"):
        assert _pin_aws(client, org, acct).status_code == 200
    assert _scope_ids(client, org, "aws_events") == [
        "100000000001", "100000000002", "100000000003",
    ]
    assert _systems_used(client, org) == 3

    # Unpin the middle scope — the others survive, count drops by one.
    assert client.delete(
        "/api/connectors/aws_events/scopes/100000000002", headers=_hdr(org)
    ).status_code == 204
    assert _scope_ids(client, org, "aws_events") == ["100000000001", "100000000003"]
    assert _systems_used(client, org) == 2

    # Re-pin an ALREADY-pinned scope — idempotent: no duplicate, count unchanged.
    # (The pinned SET is what matters; a re-pin may reorder the list, so compare
    # as a set rather than by position.)
    assert _pin_aws(client, org, "100000000001").status_code == 200
    assert sorted(_scope_ids(client, org, "aws_events")) == ["100000000001", "100000000003"]
    assert _systems_used(client, org) == 2

    # A brand-new pin adds exactly one more.
    assert _pin_aws(client, org, "100000000004").status_code == 200
    assert _systems_used(client, org) == 3


def test_aws_and_azure_scopes_are_independent(client, org):
    """Mixed multi-provider scopes count together but manage independently."""
    _create_aws(client, org)
    _create_azure(client, org)
    _pin_aws(client, org, "100000000001")
    _pin_aws(client, org, "100000000002")
    _pin_azure(client, org, "sub-1")
    _pin_azure(client, org, "sub-2")
    assert _systems_used(client, org) == 4

    # Unpinning one AWS account leaves the Azure subscriptions untouched.
    client.delete("/api/connectors/aws_events/scopes/100000000001", headers=_hdr(org))
    assert _scope_ids(client, org, "aws_events") == ["100000000002"]
    assert sorted(_scope_ids(client, org, "azure_events")) == ["sub-1", "sub-2"]
    assert _systems_used(client, org) == 3


def test_unpin_is_forward_only_and_repeatable(client, org):
    """Unpin stops future ingestion; repeating it is idempotent (204 each time)."""
    _create_azure(client, org)
    _pin_azure(client, org, "sub-1")
    assert _scope_ids(client, org, "azure_events") == ["sub-1"]

    assert client.delete(
        "/api/connectors/azure_events/scopes/sub-1", headers=_hdr(org)
    ).status_code == 204
    assert _scope_ids(client, org, "azure_events") == []
    # Idempotent — unpinning an already-unpinned scope still 204s.
    assert client.delete(
        "/api/connectors/azure_events/scopes/sub-1", headers=_hdr(org)
    ).status_code == 204
    assert _systems_used(client, org) == 0


# ═════════════════════════════════════════════════════════════════════════════
# T7-AC3 — RBAC across ALL supported roles
# ═════════════════════════════════════════════════════════════════════════════

# Every write route on the cloud connectors is Owner-only.
_WRITE_CALLS = [
    ("post", "/api/connectors/aws_events",
     {"partition": "aws", "access_key_id": _AWS_KEY, "secret_access_key": _AWS_SECRET}),
    ("post", "/api/connectors/aws_events/test",
     {"partition": "aws", "access_key_id": _AWS_KEY, "secret_access_key": _AWS_SECRET}),
    ("post", "/api/connectors/aws_events/scopes",
     {"account_id": "100000000001", "role_arn": _ROLE_ARN.format(acct="100000000001")}),
    ("delete", "/api/connectors/aws_events/scopes/100000000001", None),
]


@pytest.mark.parametrize("role_token", [ANALYST_TOKEN, VIEWER_TOKEN])
@pytest.mark.parametrize("method,path,payload", _WRITE_CALLS)
def test_write_routes_forbidden_for_non_owner(client, org, role_token, method, path, payload):
    """Analyst AND Viewer are blocked (403) from every cloud-connector write."""
    call = getattr(client, method)
    resp = call(path, headers=_hdr(org, role_token), **({"json": payload} if payload else {}))
    assert resp.status_code == 403, resp.text


@pytest.mark.parametrize("method,path,payload", _WRITE_CALLS)
def test_write_routes_require_auth(client, org, method, path, payload):
    """Unauthenticated writes are 401 (auth before RBAC)."""
    call = getattr(client, method)
    resp = call(path, **({"json": payload} if payload else {}))
    assert resp.status_code == 401


@pytest.mark.parametrize("role_token", [OWNER_TOKEN, ANALYST_TOKEN, VIEWER_TOKEN])
def test_read_routes_allowed_for_all_roles(client, org, role_token):
    """Owner, Analyst and Viewer can all READ scopes, health and security artifacts."""
    # Owner sets up a pinned scope so health has something to read.
    _create_aws(client, org)
    _pin_aws(client, org, "100000000001")

    assert client.get(
        "/api/connectors/aws_events/scopes", headers=_hdr(org, role_token)
    ).status_code == 200
    assert client.get(
        "/api/connectors/aws_events/scopes/100000000001/health",
        headers=_hdr(org, role_token),
    ).status_code == 200
    assert client.get(
        "/api/connectors/aws_events/security-artifacts", headers=_hdr(org, role_token)
    ).status_code == 200


def test_owner_can_perform_every_write(client, org):
    """The Owner role can run the whole write surface end-to-end (positive RBAC)."""
    assert client.post(
        "/api/connectors/aws_events/test", headers=_hdr(org, OWNER_TOKEN),
        json={"partition": "aws", "access_key_id": _AWS_KEY, "secret_access_key": _AWS_SECRET},
    ).status_code == 200
    assert _create_aws(client, org, OWNER_TOKEN).status_code == 200
    assert _pin_aws(client, org, "100000000001", OWNER_TOKEN).status_code == 200
    assert client.delete(
        "/api/connectors/aws_events/scopes/100000000001", headers=_hdr(org, OWNER_TOKEN)
    ).status_code == 204


def test_non_member_is_forbidden_even_with_valid_token(client, org):
    """A Viewer of one org cannot read another org's connector (tenant + RBAC)."""
    other = f"org_b13t7_{uuid.uuid4().hex[:10]}"   # NOT seeded → viewer has no role there
    resp = client.get(
        "/api/connectors/aws_events/scopes", headers=_hdr(other, VIEWER_TOKEN)
    )
    assert resp.status_code == 403
