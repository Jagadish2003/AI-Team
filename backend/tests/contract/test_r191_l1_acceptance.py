"""R191-L1 / T6 — Consolidated acceptance suite for Licensing Completion & Hardening.

This suite maps every acceptance criterion of Section 3 of the 1.9.1 release
stories to a concrete, executable check against the BUILT behaviour (not the task
list), so "done" is verified end to end in one place:

AC1  A v2 key issued for org A validates in org A; the same key in org B is
     invalid: org_mismatch, with the reason visible in the status API.
AC2  Two trusted keys with distinct kids both verify; an unknown kid yields
     invalid: unknown_key.
AC3  A v1-shaped payload (no org_id / kid) is rejected as
     unsupported_payload_version.
AC4  With no license installed, connecting systems succeeds up to
     UNLICENSED_SYSTEM_CAP and is refused beyond it with licensing-specific
     wording; installing a valid license lifts the cap to the payload's
     max_systems.
AC5  deployment_type is parsed, exposed in license status, and stamped into the
     run/telemetry context (consumed by L2).
AC6  Run gating behaviour is unchanged for all statuses (regression over the
     allow-list).

Per-task suites remain the primary coverage; this suite is the story-level safety
net that ties them together:
  * AC1 — test_licensing.py / test_license_routes.py (T2)
  * AC2 — test_license_kid_verification.py (T3)
  * AC3 — test_licensing.py (T4)
  * AC4 — test_license_max_systems.py (T5)
  * AC5 — test_generate_license.py / test_licensing.py (T1) + this file (run stamp)
  * AC6 — test_license_gate.py (T5 gate)

Crypto is exercised with a throwaway Ed25519 keypair (the real CloudFulcrum
private key is not in the repo): keys are minted with the exact issuing encoding
and verified either with an explicit public key or via the LICENSE_PUBLIC_KEY /
LICENSE_TRUSTED_KEYS config the app already honours.
"""
from __future__ import annotations

import base64
import datetime
import json
import uuid
from datetime import datetime as _dt, timezone

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from app import db, license_limits, licensing
from app.license_runtime import get_deployment_type, read_org_license, set_org_license_key
from app.licensing import (
    DEFAULT_KID,
    LicenseStatus,
    validate_license,
    verify_license_signature,
)

AUTH = {"Authorization": "Bearer dev-token-change-me"}
DEV_USER = "dev-token-change-me"
STATUS_PATH = "/api/license"
UPDATE_PATH = "/api/license/update-key"
GATE = "app.middleware.license_gate.get_current_license_status"
LIMITS_STATUS = "app.license_limits.get_current_license_status"


# ---------------------------------------------------------------------------
# Crypto + fixture helpers
# ---------------------------------------------------------------------------
def _pub_pem(priv: Ed25519PrivateKey) -> str:
    return priv.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()


def _iso(days_from_today: int) -> str:
    return (datetime.date.today() + datetime.timedelta(days=days_from_today)).isoformat()


def _mint(
    priv: Ed25519PrivateKey,
    *,
    org_id: str | None = "org-A",
    kid: str | None = DEFAULT_KID,
    deployment_type: str | None = None,
    expires_at: str | None = None,
    grace_days: int = 14,
    max_systems: int | None = None,
) -> str:
    """Mint a signed key with the exact issuing encoding (sort_keys + b64 + Ed25519).

    Defaults to a v2-shaped payload (org_id + kid). Pass ``org_id=None`` and/or
    ``kid=None`` to mint a v1-shaped payload for the AC3 rejection checks."""
    payload: dict = {
        "customer": "City National Bank",
        "license_id": "cnb-2026-001",
        "issued_at": "2026-01-01",
        "expires_at": expires_at or _iso(120),
        "term_months": 12,
        "grace_days": grace_days,
        "limits": {"max_systems": max_systems, "max_workspaces": None, "enabled_packs": None},
    }
    if org_id is not None:
        payload["org_id"] = org_id
    if kid is not None:
        payload["kid"] = kid
    if deployment_type is not None:
        payload["deployment_type"] = deployment_type
    payload_b64 = base64.b64encode(json.dumps(payload, sort_keys=True).encode()).decode()
    sig_b64 = base64.b64encode(priv.sign(payload_b64.encode())).decode()
    return f"{payload_b64}.{sig_b64}"


def _owner_headers() -> dict:
    """Seed the dev user as owner of a fresh org; return request headers."""
    from app.rbac import _ensure_members_table

    _ensure_members_table()
    org_id = f"l1acc_{uuid.uuid4().hex[:8]}"
    con = db.connect()
    try:
        con.execute(
            "INSERT INTO workspace_members (org_id, user_id, role, created_at) "
            "VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (org_id, user_id) DO UPDATE SET role=EXCLUDED.role, created_at=EXCLUDED.created_at",
            (org_id, DEV_USER, "owner", _dt.now(timezone.utc).isoformat()),
        )
        con.commit()
    finally:
        con.close()
    return {**AUTH, "X-Org-Id": org_id}


def _fresh_org() -> str:
    return f"l1acc_ms_{uuid.uuid4().hex[:10]}"


def _seed_catalog(connector_ids) -> None:
    for cid in connector_ids:
        db.upsert("connectors", cid, {"id": cid, "name": cid.title(), "status": "disconnected"})


@pytest.fixture
def keypair():
    priv = Ed25519PrivateKey.generate()
    return priv, priv.public_key()


# ===========================================================================
# AC1 — Org binding: a v2 key validates in its org, is org_mismatch elsewhere,
#       and the reason is visible in the status API.
# ===========================================================================
class TestAC1_OrgBinding:
    def test_same_key_validates_in_org_a_mismatches_in_org_b(self, keypair):
        priv, pub = keypair
        key = _mint(priv, org_id="org-A")
        assert validate_license(key, public_key=pub, installation_org_id="org-A")["status"] == LicenseStatus.VALID
        assert validate_license(key, public_key=pub, installation_org_id="org-B") == {
            "status": LicenseStatus.INVALID,
            "reason": "org_mismatch",
        }

    def test_org_mismatch_beats_expiry(self, keypair):
        """The binding check runs before the date logic — a wrong-org key is
        org_mismatch even when expired, never readonly."""
        priv, pub = keypair
        key = _mint(priv, org_id="org-A", expires_at=_iso(-999))
        assert validate_license(key, public_key=pub, installation_org_id="org-B")["reason"] == "org_mismatch"

    def test_status_api_surfaces_org_mismatch_reason(self, client: TestClient, monkeypatch):
        """AC1: an installed key bound to a different org surfaces on GET /api/license
        as status=invalid, reason=org_mismatch, so the UI can explain it."""
        priv = Ed25519PrivateKey.generate()
        monkeypatch.setenv("LICENSE_PUBLIC_KEY", _pub_pem(priv))
        headers = _owner_headers()
        org_id = headers["X-Org-Id"]
        set_org_license_key(org_id, _mint(priv, org_id="some-other-org"))

        body = client.get(STATUS_PATH, headers=headers).json()
        assert body["status"] == "invalid"
        assert body["reason"] == "org_mismatch"


# ===========================================================================
# AC2 — Key-set (kid) verification against the configured trusted key set.
# ===========================================================================
class TestAC2_KidVerification:
    @pytest.fixture(autouse=True)
    def _clean_env(self, monkeypatch):
        monkeypatch.delenv(licensing.LICENSE_TRUSTED_KEYS_ENV, raising=False)
        monkeypatch.delenv(licensing.LICENSE_PUBLIC_KEY_ENV, raising=False)

    def test_two_trusted_kids_each_verify(self, monkeypatch):
        priv_a, priv_b = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
        monkeypatch.setenv(
            licensing.LICENSE_TRUSTED_KEYS_ENV,
            json.dumps({"cf-a": _pub_pem(priv_a), "cf-b": _pub_pem(priv_b)}),
        )
        assert validate_license(_mint(priv_a, kid="cf-a"))["status"] == LicenseStatus.VALID
        assert validate_license(_mint(priv_b, kid="cf-b"))["status"] == LicenseStatus.VALID

    def test_unknown_kid_is_unknown_key(self, monkeypatch):
        priv_a = Ed25519PrivateKey.generate()
        monkeypatch.setenv(licensing.LICENSE_TRUSTED_KEYS_ENV, json.dumps({"cf-a": _pub_pem(priv_a)}))
        assert validate_license(_mint(priv_a, kid="cf-does-not-exist")) == {
            "status": LicenseStatus.INVALID,
            "reason": "unknown_key",
        }

    def test_known_kid_wrong_signer_is_signature_or_format(self, monkeypatch):
        priv_a, attacker = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
        monkeypatch.setenv(licensing.LICENSE_TRUSTED_KEYS_ENV, json.dumps({"cf-a": _pub_pem(priv_a)}))
        assert validate_license(_mint(attacker, kid="cf-a")) == {
            "status": LicenseStatus.INVALID,
            "reason": "signature_or_format",
        }


# ===========================================================================
# AC3 — Payload v1 rejection: a payload missing org_id and/or kid is invalid:
#       unsupported_payload_version, ahead of org binding and the date logic.
# ===========================================================================
class TestAC3_V1Rejection:
    def test_v1_payload_missing_both_is_unsupported(self, keypair):
        priv, pub = keypair
        assert validate_license(_mint(priv, org_id=None, kid=None), public_key=pub) == {
            "status": LicenseStatus.INVALID,
            "reason": "unsupported_payload_version",
        }

    def test_missing_org_id_only_is_unsupported(self, keypair):
        priv, pub = keypair
        assert validate_license(_mint(priv, org_id=None), public_key=pub)["reason"] == "unsupported_payload_version"

    def test_missing_kid_only_is_unsupported(self, keypair):
        priv, pub = keypair
        assert validate_license(_mint(priv, kid=None), public_key=pub)["reason"] == "unsupported_payload_version"

    def test_v1_rejection_precedes_org_binding_and_date_logic(self, keypair):
        """A v1 key (no org_id/kid), long expired, against a named org is
        unsupported_payload_version — not org_mismatch, not readonly."""
        priv, pub = keypair
        key = _mint(priv, org_id=None, kid=None, expires_at=_iso(-999))
        assert validate_license(key, public_key=pub, installation_org_id="org-B")["reason"] == "unsupported_payload_version"

    def test_low_level_primitive_still_accepts_v1(self, keypair):
        """verify_license_signature (the primitive) is unchanged — only the
        status validator rejects v1. Documents the boundary."""
        priv, pub = keypair
        assert verify_license_signature(_mint(priv, org_id=None, kid=None), public_key=pub) is not None


# ===========================================================================
# AC4 — Unlicensed connection cap (UNLICENSED_SYSTEM_CAP, default 2).
# ===========================================================================
class TestAC4_UnlicensedCap:
    def test_no_license_returns_the_cap_not_unlimited(self, monkeypatch):
        monkeypatch.setattr(LIMITS_STATUS, lambda *a, **k: {"status": "readonly", "reason": "no_license"})
        assert license_limits.get_max_systems("any_org") == 2
        assert license_limits.DEFAULT_UNLICENSED_SYSTEM_CAP == 2

    def test_valid_license_without_max_systems_stays_unlimited(self, monkeypatch):
        monkeypatch.setattr(LIMITS_STATUS, lambda *a, **k: {"status": "valid", "payload": {"limits": {}}})
        assert license_limits.get_max_systems("any_org") is None

    def test_cap_is_configurable(self, monkeypatch):
        monkeypatch.setenv("UNLICENSED_SYSTEM_CAP", "4")
        assert license_limits.get_unlicensed_system_cap() == 4

    def test_unlicensed_connect_up_to_cap_then_refused(self, client: TestClient, monkeypatch):
        from app.rbac import seed_owner

        org = _fresh_org()
        seed_owner(org, DEV_USER)
        _seed_catalog(["sys1", "sys2", "sys3"])
        monkeypatch.setattr(LIMITS_STATUS, lambda *a, **k: {"status": "readonly", "reason": "no_license"})
        hdr = {**AUTH, "X-Org-Id": org}

        assert client.post("/api/connectors/sys1/connect", json={}, headers=hdr).status_code == 200
        assert client.post("/api/connectors/sys2/connect", json={}, headers=hdr).status_code == 200
        blocked = client.post("/api/connectors/sys3/connect", json={}, headers=hdr)
        assert blocked.status_code == 402
        detail = blocked.json()["detail"]
        assert detail["reason"] == license_limits.BLOCK_REASON
        assert "license" in detail["detail"].lower()

    def test_installing_license_lifts_the_cap(self, client: TestClient, monkeypatch):
        from app.rbac import seed_owner

        org = _fresh_org()
        seed_owner(org, DEV_USER)
        _seed_catalog(["sys1", "sys2", "sys3"])
        hdr = {**AUTH, "X-Org-Id": org}

        monkeypatch.setattr(LIMITS_STATUS, lambda *a, **k: {"status": "readonly", "reason": "no_license"})
        assert client.post("/api/connectors/sys1/connect", json={}, headers=hdr).status_code == 200
        assert client.post("/api/connectors/sys2/connect", json={}, headers=hdr).status_code == 200
        assert client.post("/api/connectors/sys3/connect", json={}, headers=hdr).status_code == 402

        monkeypatch.setattr(
            LIMITS_STATUS, lambda *a, **k: {"status": "valid", "payload": {"limits": {"max_systems": 5}}}
        )
        assert client.post("/api/connectors/sys3/connect", json={}, headers=hdr).status_code == 200


# ===========================================================================
# AC5 — deployment_type is parsed, exposed in status, and stamped into the
#       run/telemetry context (consumed by L2).
# ===========================================================================
class TestAC5_DeploymentType:
    def test_validate_license_lifts_deployment_type(self, keypair):
        priv, pub = keypair
        result = validate_license(_mint(priv, deployment_type="customer_hosted"), public_key=pub)
        assert result["status"] == LicenseStatus.VALID
        assert result["deployment_type"] == "customer_hosted"
        assert result["payload"]["deployment_type"] == "customer_hosted"

    def test_deployment_type_none_when_absent(self, keypair):
        priv, pub = keypair
        assert validate_license(_mint(priv), public_key=pub)["deployment_type"] is None

    def test_get_deployment_type_resolver(self, monkeypatch):
        """The run/telemetry-context stamp source resolves the license's
        deployment_type, and None when there is no verifiable license."""
        monkeypatch.setattr(
            "app.license_runtime.get_current_license_status",
            lambda **k: {"status": "valid", "deployment_type": "saas", "payload": {}},
        )
        assert get_deployment_type("o") == "saas"
        monkeypatch.setattr(
            "app.license_runtime.get_current_license_status",
            lambda **k: {"status": "readonly", "reason": "no_license"},
        )
        assert get_deployment_type("o") is None

    def test_status_api_exposes_deployment_type(self, client: TestClient, monkeypatch):
        priv = Ed25519PrivateKey.generate()
        monkeypatch.setenv("LICENSE_PUBLIC_KEY", _pub_pem(priv))
        headers = _owner_headers()
        org_id = headers["X-Org-Id"]
        key = _mint(priv, org_id=org_id, deployment_type="customer_hosted")

        install = client.post(UPDATE_PATH, json={"key": key}, headers=headers)
        assert install.status_code == 200, install.text
        assert install.json()["deployment_type"] == "customer_hosted"
        assert client.get(STATUS_PATH, headers=headers).json()["deployment_type"] == "customer_hosted"

    def test_runner_stamps_deployment_type_into_run_telemetry(self, monkeypatch):
        """AC5: the discovery runner stamps the license deployment_type into the
        run.started / run.completed telemetry context (what L2 billing consumes)."""
        from discovery import runner

        captured: list = []
        monkeypatch.setattr(runner, "record_event", lambda et, payload=None: captured.append((et, payload or {})))
        monkeypatch.setattr("app.license_runtime.get_deployment_type", lambda org_id=None: "customer_hosted")

        runner.run(mode="offline", org_id=f"l1acc_run_{uuid.uuid4().hex[:8]}", run_id=f"run_{uuid.uuid4().hex[:8]}")

        started = [p for et, p in captured if et == "run.started"]
        completed = [p for et, p in captured if et == "run.completed"]
        assert started, "run.started must be emitted"
        assert started[0].get("deployment_type") == "customer_hosted"
        assert completed, "run.completed must be emitted"
        assert all(p.get("deployment_type") == "customer_hosted" for p in completed)


# ===========================================================================
# AC6 — Run-gate regression across ALL statuses (allow-list stands): valid/grace
#       run; readonly/invalid/no_license/clock_rollback are blocked (402).
# ===========================================================================
class TestAC6_RunGateRegression:
    @pytest.mark.parametrize("status", [LicenseStatus.VALID, LicenseStatus.GRACE])
    def test_run_allowed_for_healthy_statuses(self, client: TestClient, monkeypatch, status):
        monkeypatch.setattr(GATE, lambda *a, **k: {"status": status})
        resp = client.post("/api/runs/start", json={}, headers=AUTH)
        # The gate must not license-block a healthy status; the route may 200/422
        # on the body, but never 402 with the license reason.
        assert resp.status_code != 402

    @pytest.mark.parametrize(
        "status,reason",
        [
            (LicenseStatus.READONLY, "no_license"),
            (LicenseStatus.INVALID, "signature_or_format"),
            (LicenseStatus.READONLY, "clock_rollback"),
            (LicenseStatus.INVALID, "unsupported_payload_version"),
            (LicenseStatus.INVALID, "org_mismatch"),
        ],
    )
    def test_run_blocked_for_unhealthy_statuses(self, client: TestClient, monkeypatch, status, reason):
        monkeypatch.setattr(GATE, lambda *a, **k: {"status": status, "reason": reason})
        resp = client.post("/api/runs/start", json={}, headers=AUTH)
        assert resp.status_code == 402
        body = resp.json()
        assert body["reason"] == "license_inactive"
        assert body["licenseStatus"] == status

    def test_reads_never_gated_regardless_of_status(self, client: TestClient, monkeypatch):
        """AC6 / §5: reads stay available in read-only so findings remain viewable."""
        monkeypatch.setattr(GATE, lambda *a, **k: {"status": LicenseStatus.READONLY, "reason": "no_license"})
        assert client.get("/api/runs", headers=AUTH).status_code == 200

    def test_unknown_future_status_fails_closed(self, client: TestClient, monkeypatch):
        """The gate is an allow-list: an unrecognised status can never open it."""
        monkeypatch.setattr(GATE, lambda *a, **k: {"status": "some_new_status"})
        assert client.post("/api/runs/start", json={}, headers=AUTH).status_code == 402
