"""MSP-B13 / AT-745 (T3) — Cloud Connector Onboarding config/validation routes.

Contract tests for the AWS/Azure Event connector onboarding routes added in
``app/routes_cloud_connectors.py``:

    POST   /api/connectors/{aws_events|azure_events}          create + vault write
    POST   /api/connectors/{id}/test                          auth + reachability probe
    GET    /api/connectors/{id}/scopes                        list pinned + candidates
    POST   /api/connectors/{id}/scopes                        pin (validated)
    DELETE /api/connectors/{id}/scopes/{scope}                unpin (forward-only)
    GET    /api/connectors/{id}/scopes/{scope}/health         per-scope health

Verifies the AT-745 acceptance criteria at the ROUTE layer:
  T3-AC1 — connector configuration endpoints support AWS and Azure connections
  T3-AC2 — test-connection validates authentication + connectivity BEFORE save
  T3-AC3 — scope pin / unpin / retrieval APIs function correctly
  T3-AC4 — scope health endpoint returns connector health information
plus the load-bearing B13 rules: write-only secrets (AC2), RBAC (AC1),
forward-only activation (AC4/AC6), and partition/environment selection (AC8).

The provider probes are substituted with deterministic fakes so the suite needs
no boto3 / no AWS or Azure account. FAKE CREDENTIALS: every value below is a
non-real, test-only credential.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from cryptography.fernet import Fernet

from app import db
from app.auth.vault import get_static_credential, revoke_static_credential
import app.routes_cloud_connectors as rcc

# One vault key for the whole module (the shared session DB persists rows).
_VAULT_KEY = Fernet.generate_key().decode()

OWNER = {"Authorization": "Bearer dev-token-change-me"}    # seeded owner of 'default'
VIEWER = {"Authorization": "Bearer viewer-token"}           # seeded viewer of 'default'
NO_AUTH: dict = {}

# Fake, non-real credentials.
_AWS_KEY = "AKIAFAKEEXAMPLE01234"
_AWS_SECRET = "FAKE/aws/secret/key/abcdefghijklmnopqrstuv"
_AZ_TENANT = "11111111-1111-1111-1111-111111111111"
_AZ_CLIENT = "22222222-2222-2222-2222-222222222222"
_AZ_SECRET = "FAKE-azure-sp-secret-0123456789"
_ROLE_ARN = "arn:aws:iam::123456789012:role/AgentIQReadOnly"


def _seed_member(org_id: str, user_id: str, role: str) -> None:
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(
            """
            INSERT INTO workspace_members (org_id, user_id, role, created_at)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (org_id, user_id)
            DO UPDATE SET role = EXCLUDED.role, is_deleted = FALSE
            """,
            (org_id, user_id, role, datetime.now(timezone.utc).isoformat()),
        )
        con.commit()
    finally:
        con.close()


@pytest.fixture(autouse=True)
def _vault_key(monkeypatch):
    monkeypatch.setenv("CREDENTIAL_VAULT_KEY", _VAULT_KEY)
    yield


@pytest.fixture(autouse=True, scope="module")
def _seed_roles():
    from app.rbac import seed_owner

    seed_owner("default", "dev-token-change-me")
    _seed_member("default", "viewer-token", "viewer")


@pytest.fixture(autouse=True)
def _clean_state():
    """Reset both cloud connectors' per-org state + vault creds around each test."""
    def _reset():
        for cid in ("aws_events", "azure_events"):
            db.org_connector_set(
                "default",
                cid,
                {"status": "not_configured", "configured": False, "scopes": []},
            )
            revoke_static_credential("default", cid)
        revoke_static_credential("default", "aws_events:account:123456789012")

    _reset()
    yield
    _reset()


@pytest.fixture(autouse=True)
def _fake_probes(monkeypatch):
    """Substitute the provider probes so no boto3 / network is needed by default.

    Each test that wants a failure overrides the relevant probe again.
    """
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


# ─────────────────────────────────────────────────────────────────────────────
# Auth + RBAC (B13 AC1)
# ─────────────────────────────────────────────────────────────────────────────


def test_create_requires_auth(client):
    r = client.post("/api/connectors/aws_events", json={
        "access_key_id": _AWS_KEY, "secret_access_key": _AWS_SECRET,
    })
    assert r.status_code == 401


def test_create_forbidden_for_viewer(client):
    r = client.post(
        "/api/connectors/aws_events",
        headers=VIEWER,
        json={"access_key_id": _AWS_KEY, "secret_access_key": _AWS_SECRET},
    )
    assert r.status_code == 403
    assert get_static_credential("default", "aws_events") is None


def test_scope_list_allows_viewer(client):
    """Analyst/Viewer can see health/scopes (read-only) — AC1."""
    r = client.get("/api/connectors/aws_events/scopes", headers=VIEWER)
    assert r.status_code == 200


def test_pin_forbidden_for_viewer(client):
    r = client.post(
        "/api/connectors/aws_events/scopes",
        headers=VIEWER,
        json={"account_id": "123456789012", "role_arn": _ROLE_ARN},
    )
    assert r.status_code == 403


def test_unknown_cloud_connector_is_404(client):
    assert client.post(
        "/api/connectors/gcp_events", headers=OWNER, json={}
    ).status_code == 404


# ─────────────────────────────────────────────────────────────────────────────
# T3-AC1 — create AWS + Azure connections, vaulted + write-only (B13 AC2)
# ─────────────────────────────────────────────────────────────────────────────


def test_create_aws_connection_vaults_and_returns_metadata_only(client):
    r = client.post(
        "/api/connectors/aws_events",
        headers=OWNER,
        json={
            "partition": "aws",
            "access_key_id": _AWS_KEY,
            "secret_access_key": _AWS_SECRET,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["connector_id"] == "aws_events"
    assert body["provider"] == "aws"
    assert body["configured"] is True
    assert body["status"] == "connected"
    assert body["partition"] == "aws"
    # Write-only: the response never carries the secret/key value (AC2).
    assert _AWS_SECRET not in r.text
    assert _AWS_KEY not in r.text
    # The secret lands Fernet-encrypted in the vault and round-trips.
    rec = get_static_credential("default", "aws_events")
    assert rec is not None
    assert rec.username == _AWS_KEY
    assert rec.secret == _AWS_SECRET


def test_create_azure_connection_vaults_and_records_environment(client):
    r = client.post(
        "/api/connectors/azure_events",
        headers=OWNER,
        json={
            "environment": "AzureUSGovernment",
            "mode": "lighthouse",
            "tenant_id": _AZ_TENANT,
            "client_id": _AZ_CLIENT,
            "client_secret": _AZ_SECRET,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["provider"] == "azure"
    assert body["environment"] == "AzureUSGovernment"    # partition/env selection (AC8)
    assert body["mode"] == "lighthouse"
    assert _AZ_SECRET not in r.text                       # write-only (AC2)
    rec = get_static_credential("default", "azure_events")
    assert rec is not None
    assert rec.username == _AZ_CLIENT
    assert rec.secret == _AZ_SECRET
    assert rec.base_url == _AZ_TENANT


def test_create_aws_missing_secret_is_400(client):
    r = client.post(
        "/api/connectors/aws_events",
        headers=OWNER,
        json={"access_key_id": _AWS_KEY, "secret_access_key": ""},
    )
    assert r.status_code == 400
    assert "secret access key" in r.json()["detail"]
    assert get_static_credential("default", "aws_events") is None


def test_create_aws_invalid_partition_is_400(client):
    r = client.post(
        "/api/connectors/aws_events",
        headers=OWNER,
        json={
            "partition": "aws-cn",
            "access_key_id": _AWS_KEY,
            "secret_access_key": _AWS_SECRET,
        },
    )
    assert r.status_code == 400
    assert get_static_credential("default", "aws_events") is None


def test_create_azure_invalid_environment_is_400(client):
    r = client.post(
        "/api/connectors/azure_events",
        headers=OWNER,
        json={
            "environment": "AzureChinaCloud",
            "tenant_id": _AZ_TENANT,
            "client_id": _AZ_CLIENT,
            "client_secret": _AZ_SECRET,
        },
    )
    assert r.status_code == 400
    assert get_static_credential("default", "azure_events") is None


# ─────────────────────────────────────────────────────────────────────────────
# T3-AC2 — test-connection validates BEFORE save (B13 AC3)
# ─────────────────────────────────────────────────────────────────────────────


def test_aws_test_connection_success(client):
    r = client.post(
        "/api/connectors/aws_events/test",
        headers=OWNER,
        json={"access_key_id": _AWS_KEY, "secret_access_key": _AWS_SECRET, "partition": "aws"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    # A test must NOT persist anything (validate-before-save).
    assert get_static_credential("default", "aws_events") is None


def test_aws_test_connection_seeded_auth_failure(client, monkeypatch):
    def _bad(**kwargs):
        raise rcc.CloudProbeError(
            "authentication_failed", "AWS rejected the credentials."
        )

    monkeypatch.setattr(rcc, "probe_aws_hub_credentials", _bad)
    r = client.post(
        "/api/connectors/aws_events/test",
        headers=OWNER,
        json={"access_key_id": _AWS_KEY, "secret_access_key": "wrong", "partition": "aws"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is False
    assert body["reason"] == "authentication_failed"
    assert "rejected" in body["message"].lower()


def test_probe_partition_guard_rejects_bad_partition():
    """The real probe's partition guard rejects an unknown/contradictory partition (AC8)."""
    with pytest.raises(rcc.CloudProbeError) as ei:
        rcc._validate_aws_partition("aws-cn", None)
    assert ei.value.reason == "invalid_partition"

    # A GovCloud region under the commercial partition is a contradiction.
    with pytest.raises(rcc.CloudProbeError):
        rcc._validate_aws_partition("aws", "us-gov-west-1")


def test_azure_test_connection_success(client):
    r = client.post(
        "/api/connectors/azure_events/test",
        headers=OWNER,
        json={
            "environment": "AzureCloud",
            "tenant_id": _AZ_TENANT,
            "client_id": _AZ_CLIENT,
            "client_secret": _AZ_SECRET,
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    assert get_static_credential("default", "azure_events") is None


def test_azure_test_connection_seeded_secret_failure(client, monkeypatch):
    async def _bad(org_id, *, service_principal, config):
        raise rcc.CloudProbeError(
            "authentication_failed",
            "Azure AD rejected the service principal. Check the client secret.",
        )

    monkeypatch.setattr(rcc, "probe_azure_service_principal", _bad)
    r = client.post(
        "/api/connectors/azure_events/test",
        headers=OWNER,
        json={
            "environment": "AzureCloud",
            "tenant_id": _AZ_TENANT,
            "client_id": _AZ_CLIENT,
            "client_secret": "expired",
        },
    )
    body = r.json()
    assert r.status_code == 200
    assert body["ok"] is False
    assert body["reason"] == "authentication_failed"


def test_test_connection_missing_credentials_reports_verdict(client):
    r = client.post("/api/connectors/aws_events/test", headers=OWNER, json={})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["reason"] == "missing_credentials"


def test_test_connection_forbidden_for_viewer(client):
    r = client.post(
        "/api/connectors/aws_events/test",
        headers=VIEWER,
        json={"access_key_id": _AWS_KEY, "secret_access_key": _AWS_SECRET},
    )
    assert r.status_code == 403


# ─────────────────────────────────────────────────────────────────────────────
# T3-AC3 — scope pin / unpin / retrieval (B13 AC4/AC6)
# ─────────────────────────────────────────────────────────────────────────────


def _connect_aws(client):
    client.post(
        "/api/connectors/aws_events",
        headers=OWNER,
        json={"partition": "aws", "access_key_id": _AWS_KEY, "secret_access_key": _AWS_SECRET},
    )


def _connect_azure(client):
    client.post(
        "/api/connectors/azure_events",
        headers=OWNER,
        json={
            "environment": "AzureCloud", "mode": "lighthouse",
            "tenant_id": _AZ_TENANT, "client_id": _AZ_CLIENT, "client_secret": _AZ_SECRET,
        },
    )


def test_pin_before_connection_is_400(client):
    r = client.post(
        "/api/connectors/aws_events/scopes",
        headers=OWNER,
        json={"account_id": "123456789012", "role_arn": _ROLE_ARN},
    )
    assert r.status_code == 400


def test_pin_aws_account_by_role_arn(client):
    _connect_aws(client)
    r = client.post(
        "/api/connectors/aws_events/scopes",
        headers=OWNER,
        json={
            "account_id": "123456789012",
            "role_arn": _ROLE_ARN,
            "external_id": "ext-123",
            "regions": ["us-east-1"],
            "label": "Prod",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    scopes = body["scopes"]
    assert len(scopes) == 1
    s = scopes[0]
    assert s["scope_id"] == "123456789012"
    assert s["kind"] == "aws_account"
    assert s["role_arn"] == _ROLE_ARN
    assert s["external_id_set"] is True         # presence only, never the value
    assert s["status"] == "pending"
    # The external id value never appears in the response (write-only posture).
    assert "ext-123" not in r.text


def test_pin_aws_seeded_assume_role_failure_is_400_and_not_pinned(client, monkeypatch):
    _connect_aws(client)

    def _fail(org_id, account_config):
        raise rcc.CloudProbeError(
            "assume_role_failed", "Could not assume the account role. Check the ARN."
        )

    monkeypatch.setattr(rcc, "probe_aws_assume_role", _fail)
    r = client.post(
        "/api/connectors/aws_events/scopes",
        headers=OWNER,
        json={"account_id": "123456789012", "role_arn": "arn:aws:iam::123456789012:role/Bad"},
    )
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert detail["reason"] == "assume_role_failed"
    # Nothing pinned.
    listed = client.get("/api/connectors/aws_events/scopes", headers=OWNER).json()
    assert listed["scopes"] == []


def test_pin_azure_subscription_and_list(client):
    _connect_azure(client)
    r = client.post(
        "/api/connectors/azure_events/scopes",
        headers=OWNER,
        json={"subscription_id": "sub-abc", "label": "Contoso Prod"},
    )
    assert r.status_code == 200, r.text
    scopes = r.json()["scopes"]
    assert scopes[0]["scope_id"] == "sub-abc"
    assert scopes[0]["kind"] == "azure_subscription"
    assert scopes[0]["environment"] == "AzureCloud"

    listed = client.get("/api/connectors/azure_events/scopes", headers=OWNER).json()
    assert [s["scope_id"] for s in listed["scopes"]] == ["sub-abc"]


def test_azure_candidate_not_ingested_until_pinned(client):
    """A discovered subscription is a CANDIDATE and never ingests until pinned (AC4)."""
    _connect_azure(client)
    # Seed a discovered-but-unpinned candidate on the record (as discovery would).
    rec = db.org_connector_get("default", "azure_events")
    rec["candidate_scopes"] = ["sub-candidate"]
    db.org_connector_set("default", "azure_events", rec)

    listed = client.get("/api/connectors/azure_events/scopes", headers=OWNER).json()
    assert "sub-candidate" in listed["candidates"]
    assert [s["scope_id"] for s in listed["scopes"]] == []      # not ingested

    # Owner pins it → it activates and leaves the candidate list.
    client.post(
        "/api/connectors/azure_events/scopes",
        headers=OWNER,
        json={"subscription_id": "sub-candidate"},
    )
    listed = client.get("/api/connectors/azure_events/scopes", headers=OWNER).json()
    assert [s["scope_id"] for s in listed["scopes"]] == ["sub-candidate"]
    assert "sub-candidate" not in listed["candidates"]


def test_unpin_is_forward_only_and_idempotent(client):
    _connect_azure(client)
    client.post(
        "/api/connectors/azure_events/scopes",
        headers=OWNER,
        json={"subscription_id": "sub-abc"},
    )
    # Unpin removes it from the pinned set.
    r = client.delete("/api/connectors/azure_events/scopes/sub-abc", headers=OWNER)
    assert r.status_code == 204
    listed = client.get("/api/connectors/azure_events/scopes", headers=OWNER).json()
    assert listed["scopes"] == []
    # Idempotent — a second unpin still 204s.
    assert client.delete(
        "/api/connectors/azure_events/scopes/sub-abc", headers=OWNER
    ).status_code == 204


def test_unpin_forbidden_for_viewer(client):
    _connect_azure(client)
    client.post(
        "/api/connectors/azure_events/scopes", headers=OWNER,
        json={"subscription_id": "sub-abc"},
    )
    assert client.delete(
        "/api/connectors/azure_events/scopes/sub-abc", headers=VIEWER
    ).status_code == 403


# ─────────────────────────────────────────────────────────────────────────────
# T3-AC4 — scope health (B13 AC7 — same vocabulary as run health)
# ─────────────────────────────────────────────────────────────────────────────


def test_scope_health_pending_after_pin(client):
    _connect_aws(client)
    client.post(
        "/api/connectors/aws_events/scopes",
        headers=OWNER,
        json={"account_id": "123456789012", "role_arn": _ROLE_ARN},
    )
    r = client.get(
        "/api/connectors/aws_events/scopes/123456789012/health", headers=VIEWER
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["scope_id"] == "123456789012"
    assert body["status"] == "pending"
    assert body["healthy"] is False


def test_scope_health_reflects_run_health_vocabulary(client):
    """A failed scope reads the SAME word (auth_failed) on the card as in run health."""
    _connect_aws(client)
    client.post(
        "/api/connectors/aws_events/scopes",
        headers=OWNER,
        json={"account_id": "123456789012", "role_arn": _ROLE_ARN},
    )
    # Simulate a run recording the scope failed (as the connector's run-health would).
    rec = db.org_connector_get("default", "aws_events")
    for s in rec["scopes"]:
        if s["scope_id"] == "123456789012":
            s["status"] = "auth_failed"
            s["health_message"] = "credential revoked"
            s["surfaces_failed"] = {"cloudwatch": "auth"}
    db.org_connector_set("default", "aws_events", rec)

    body = client.get(
        "/api/connectors/aws_events/scopes/123456789012/health", headers=OWNER
    ).json()
    assert body["status"] == "auth_failed"
    assert body["healthy"] is False
    assert body["surfaces_failed"] == {"cloudwatch": "auth"}


def test_scope_health_unknown_scope_is_404(client):
    _connect_aws(client)
    assert client.get(
        "/api/connectors/aws_events/scopes/nope/health", headers=OWNER
    ).status_code == 404


# ─────────────────────────────────────────────────────────────────────────────
# MSP-B13 / T5 (AT-747) — security artifacts (AC3/AC4)
# ─────────────────────────────────────────────────────────────────────────────


def test_list_security_artifacts_aws(client):
    r = client.get("/api/connectors/aws_events/security-artifacts", headers=VIEWER)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["provider"] == "aws"
    ids = {a["id"] for a in body["artifacts"]}
    assert "iam_policy" in ids               # the minimal read-only IAM policy (AC3)
    # Every artifact carries the metadata the card renders.
    for a in body["artifacts"]:
        assert a["label"] and a["description"] and a["filename"] and a["media_type"]


def test_list_security_artifacts_azure(client):
    body = client.get(
        "/api/connectors/azure_events/security-artifacts", headers=VIEWER
    ).json()
    ids = {a["id"] for a in body["artifacts"]}
    assert "rbac_role" in ids                 # the Reader RBAC role (AC4)


def test_list_security_artifacts_requires_auth(client):
    assert client.get("/api/connectors/aws_events/security-artifacts").status_code == 401


def test_list_security_artifacts_unknown_connector_404(client):
    assert client.get(
        "/api/connectors/gcp_events/security-artifacts", headers=OWNER
    ).status_code == 404


def test_download_aws_iam_policy(client):
    r = client.get(
        "/api/connectors/aws_events/security-artifacts/iam_policy", headers=VIEWER
    )
    assert r.status_code == 200, r.text
    # The real minimal IAM policy content is served (single source: deployment/).
    assert r.headers["content-type"].startswith("application/json")
    assert 'attachment; filename="aws_readonly_iam_policy.json"' in r.headers.get(
        "content-disposition", ""
    )
    assert '"Version"' in r.text and "Statement" in r.text


def test_download_azure_rbac_role(client):
    r = client.get(
        "/api/connectors/azure_events/security-artifacts/rbac_role", headers=VIEWER
    )
    assert r.status_code == 200, r.text
    assert 'filename="azure_event_connector_role.json"' in r.headers.get(
        "content-disposition", ""
    )
    # Reader-only role — the JSON names the read actions, never write/delete.
    assert "Microsoft.AlertsManagement/alerts/read" in r.text


def test_download_markdown_guide(client):
    r = client.get(
        "/api/connectors/aws_events/security-artifacts/iam_policy_guide", headers=VIEWER
    )
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/markdown")


def test_download_unknown_artifact_404(client):
    assert client.get(
        "/api/connectors/aws_events/security-artifacts/nope", headers=VIEWER
    ).status_code == 404


def test_download_requires_auth(client):
    assert client.get(
        "/api/connectors/aws_events/security-artifacts/iam_policy"
    ).status_code == 401
