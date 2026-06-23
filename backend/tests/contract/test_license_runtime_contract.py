"""Contract tests for LIC-1 / T4 (AT-345) — license validation against the real DB.

These exercise ``app.license_runtime`` through the actual per-org storage layer
(the PostgreSQL ``org_licenses`` table) and the real ``validate_license`` (T3),
complementing the hermetic unit tests in ``tests/contract/test_license_runtime.py``.

A throwaway Ed25519 keypair signs the test keys and its public key is passed
through to validation, so no real CloudFulcrum private key is required. Each test
uses a unique org id and cleans up its row so the shared session DB is unaffected.
"""
from __future__ import annotations

import base64
import datetime
import json
import uuid

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app import db
from app import license_runtime as lr
from app.licensing import LicenseStatus


def _mint(private_key: Ed25519PrivateKey, *, expires_at: str, grace_days: int = 14) -> str:
    payload = {
        "customer": "City National Bank",
        "license_id": "cnb-2026-001",
        "issued_at": "2026-01-01",
        "expires_at": expires_at,
        "term_months": 12,
        "grace_days": grace_days,
        "limits": {"max_workspaces": None, "enabled_packs": None},
    }
    payload_b64 = base64.b64encode(json.dumps(payload, sort_keys=True).encode()).decode()
    sig_b64 = base64.b64encode(private_key.sign(payload_b64.encode())).decode()
    return f"{payload_b64}.{sig_b64}"


def _delete_org(org_id: str) -> None:
    # Best-effort cleanup: each test uses a unique org id, so under the
    # least-privilege app DB role (no DELETE) this no-ops harmlessly. Autocommit so
    # a denied DELETE doesn't poison a transaction.
    con = db.connect()
    try:
        con.autocommit = True
        cur = con.cursor()
        cur.execute("DELETE FROM org_licenses WHERE org_id = %s", (org_id,))
    except Exception:
        pass
    finally:
        con.close()


@pytest.fixture
def org_id():
    oid = f"lic_rt_{uuid.uuid4().hex[:12]}"
    _delete_org(oid)
    yield oid
    _delete_org(oid)


@pytest.fixture
def keypair():
    priv = Ed25519PrivateKey.generate()
    return priv, priv.public_key()


# --------------------------------------------------------------------------
# AC6: a keyless org -> read-only 'no_license', and no row is created.
# --------------------------------------------------------------------------
def test_no_key_org_is_readonly(monkeypatch, org_id):
    monkeypatch.delenv("LICENSE_KEY", raising=False)

    result = lr.evaluate_license(org_id=org_id)

    assert result["status"] == LicenseStatus.READONLY
    assert result["reason"] == "no_license"
    # UPDATE-only persistence means a keyless org stays row-less.
    assert lr.read_org_license(org_id) is None


# --------------------------------------------------------------------------
# Valid key stored for the org -> status valid, last_seen advances. (AC3 chain)
# --------------------------------------------------------------------------
def test_valid_key_in_db_validates_and_persists(monkeypatch, keypair, org_id):
    priv, pub = keypair
    monkeypatch.delenv("LICENSE_KEY", raising=False)
    today = datetime.date.today()
    lr.set_org_license_key(
        org_id, _mint(priv, expires_at=(today + datetime.timedelta(days=200)).isoformat())
    )

    result = lr.evaluate_license(org_id=org_id, public_key=pub)

    assert result["status"] == LicenseStatus.VALID
    assert result["customer"] == "City National Bank"
    assert lr.read_org_license(org_id)["last_seen_date"] == today.isoformat()


# --------------------------------------------------------------------------
# The startup hook validates each licensed org against the real DB.
# --------------------------------------------------------------------------
def test_startup_validation_runs_for_licensed_org(monkeypatch, keypair, org_id):
    priv, pub = keypair
    monkeypatch.delenv("LICENSE_KEY", raising=False)
    monkeypatch.setattr("app.licensing.load_public_key", lambda *a, **k: pub)
    today = datetime.date.today()
    lr.set_org_license_key(
        org_id, _mint(priv, expires_at=(today + datetime.timedelta(days=200)).isoformat())
    )

    # Never raises; advances the org's baseline + caches the valid status.
    assert lr.run_startup_validation() is None
    row = lr.read_org_license(org_id)
    assert row["last_seen_date"] == today.isoformat()
    assert row["last_status"] == LicenseStatus.VALID


# --------------------------------------------------------------------------
# AC8: rolled-back clock -> read-only 'clock_rollback'; last_seen not advanced.
# --------------------------------------------------------------------------
def test_clock_rollback_against_real_db(monkeypatch, keypair, org_id):
    priv, pub = keypair
    monkeypatch.delenv("LICENSE_KEY", raising=False)
    last_seen = datetime.date.today()
    rolled_back = last_seen - datetime.timedelta(days=10)
    lr.set_org_license_key(
        org_id, _mint(priv, expires_at=(rolled_back + datetime.timedelta(days=200)).isoformat())
    )
    lr.persist_org_status(org_id, last_seen.isoformat(), None)

    result = lr.evaluate_license(org_id=org_id, today=rolled_back, public_key=pub)

    assert result["status"] == LicenseStatus.READONLY
    assert result["reason"] == "clock_rollback"
    # last_seen unchanged while the clock is inconsistent.
    assert lr.read_org_license(org_id)["last_seen_date"] == last_seen.isoformat()
