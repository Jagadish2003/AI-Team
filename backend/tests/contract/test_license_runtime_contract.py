"""Contract tests for LIC-1 / T4 (AT-345) — license validation against the real DB.

These exercise ``app.license_runtime`` through the actual ``kv_get`` / ``kv_set``
helpers (PostgreSQL ``kv`` table) and the real ``validate_license`` (T3),
complementing the hermetic unit tests in ``tests/unit/test_license_runtime.py``.

A throwaway Ed25519 keypair signs the test keys and its public key is passed
through to validation, so no real CloudFulcrum private key is required.
"""
from __future__ import annotations

import base64
import datetime
import json

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


def _reset_license_kv() -> None:
    """Clear app-global license state in the real kv table between tests."""
    db.kv_set(lr.LICENSE_KEY_KV, None)
    db.kv_set(lr.LICENSE_LAST_SEEN_KV, None)
    db.kv_set(lr.LICENSE_LAST_STATUS_KV, None)


@pytest.fixture
def keypair():
    priv = Ed25519PrivateKey.generate()
    return priv, priv.public_key()


# --------------------------------------------------------------------------
# AC6: fresh install / no key -> read-only, and last_seen is persisted.
# Exercises the exact startup hook (run_startup_validation) against the DB.
# --------------------------------------------------------------------------
def test_startup_validation_no_key_is_readonly(monkeypatch):
    monkeypatch.delenv("LICENSE_KEY", raising=False)
    _reset_license_kv()

    result = lr.run_startup_validation()

    assert result is not None
    assert result["status"] == LicenseStatus.READONLY
    assert result["reason"] == "no_license"
    assert db.kv_get(lr.LICENSE_LAST_SEEN_KV) == datetime.date.today().isoformat()


# --------------------------------------------------------------------------
# Valid key stored in the DB -> status valid, last_seen advances. (AC3 chain)
# --------------------------------------------------------------------------
def test_valid_key_in_db_validates_and_persists(monkeypatch, keypair):
    priv, pub = keypair
    monkeypatch.delenv("LICENSE_KEY", raising=False)
    _reset_license_kv()
    today = datetime.date.today()
    db.kv_set(lr.LICENSE_KEY_KV, _mint(priv, expires_at=(today + datetime.timedelta(days=200)).isoformat()))

    result = lr.evaluate_license(public_key=pub)

    assert result["status"] == LicenseStatus.VALID
    assert result["customer"] == "City National Bank"
    assert db.kv_get(lr.LICENSE_LAST_SEEN_KV) == today.isoformat()


# --------------------------------------------------------------------------
# AC8: rolled-back clock -> read-only 'clock_rollback'; last_seen not advanced.
# --------------------------------------------------------------------------
def test_clock_rollback_against_real_db(monkeypatch, keypair):
    priv, pub = keypair
    monkeypatch.delenv("LICENSE_KEY", raising=False)
    _reset_license_kv()
    last_seen = datetime.date.today()
    rolled_back = last_seen - datetime.timedelta(days=10)
    db.kv_set(lr.LICENSE_LAST_SEEN_KV, last_seen.isoformat())
    db.kv_set(lr.LICENSE_KEY_KV, _mint(priv, expires_at=(rolled_back + datetime.timedelta(days=200)).isoformat()))

    result = lr.evaluate_license(today=rolled_back, public_key=pub)

    assert result["status"] == LicenseStatus.READONLY
    assert result["reason"] == "clock_rollback"
    # last_seen unchanged while the clock is inconsistent.
    assert db.kv_get(lr.LICENSE_LAST_SEEN_KV) == last_seen.isoformat()
