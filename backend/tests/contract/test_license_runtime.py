"""Unit tests for LIC-1 / T4 (AT-345) — startup/periodic license validation.

Covers the AC-mapped behaviours of ``app.license_runtime``:
  * startup with a valid key            -> status 'valid', last_seen persisted
  * startup with no key                 -> read-only 'no_license' (AC6)
  * startup with a rolled-back clock    -> read-only 'clock_rollback' + telemetry (AC8)
  * grace / read-only expiry transitions emit transition telemetry (AC11 chain)
  * LICENSE_KEY env install path persists the key into the DB
  * clock change within the 2-day tolerance does NOT trip the guard
  * the side-effect-free read used by the gate/status route

No network, no DB, no real keypair: the kv layer is monkeypatched to an
in-memory dict, telemetry is captured, and a throwaway Ed25519 keypair signs
the test keys (the matching public key is passed through to validate_license).
"""
from __future__ import annotations

import base64
import datetime
import json

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app import license_runtime as lr
from app.licensing import LicenseStatus


# --------------------------------------------------------------------------
# Helpers / fixtures
# --------------------------------------------------------------------------
def _mint_key(private_key: Ed25519PrivateKey, *, expires_at: str, customer="City National Bank",
              grace_days: int = 14) -> str:
    """Mint a base64(payload).base64(signature) key with the issuing encoding."""
    payload = {
        "customer": customer,
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


@pytest.fixture
def lic(monkeypatch):
    """In-memory kv + captured telemetry + a throwaway keypair.

    Returns an object with: ``store`` (dict), ``events`` (list of (type, payload)),
    ``priv``/``pub`` (Ed25519 keypair), and ``mint(expires_at, ...)``.
    """
    store: dict = {}
    events: list = []

    monkeypatch.setattr(lr, "kv_get", lambda key: store.get(key))
    monkeypatch.setattr(lr, "kv_set", lambda key, value: store.__setitem__(key, value))
    monkeypatch.setattr(lr, "record_event", lambda etype, payload=None: events.append((etype, payload or {})))
    monkeypatch.delenv("LICENSE_KEY", raising=False)

    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key()

    class _Ctx:
        pass

    ctx = _Ctx()
    ctx.store = store
    ctx.events = events
    ctx.priv = priv
    ctx.pub = pub
    ctx.monkeypatch = monkeypatch
    ctx.mint = lambda **kw: _mint_key(priv, **kw)
    return ctx


def _types(events):
    return [e[0] for e in events]


# --------------------------------------------------------------------------
# AC: startup with a valid key
# --------------------------------------------------------------------------
def test_startup_with_valid_key(lic):
    today = datetime.date.today()
    lic.store[lr.LICENSE_KEY_KV] = lic.mint(expires_at=(today + datetime.timedelta(days=365)).isoformat())

    result = lr.evaluate_license(public_key=lic.pub)

    assert result["status"] == LicenseStatus.VALID
    assert result["customer"] == "City National Bank"
    # last_seen persisted as today on a clock-consistent pass.
    assert lic.store[lr.LICENSE_LAST_SEEN_KV] == today.isoformat()
    # per-check telemetry emitted for a verified key.
    assert "license.validated" in _types(lic.events)


# --------------------------------------------------------------------------
# AC6: startup with no key -> read-only "no valid license"
# --------------------------------------------------------------------------
def test_startup_with_no_key_is_readonly(lic):
    result = lr.evaluate_license(public_key=lic.pub)

    assert result["status"] == LicenseStatus.READONLY
    assert result["reason"] == "no_license"
    # nothing to report about a non-existent customer.
    assert "license.validated" not in _types(lic.events)
    assert "license.clock_anomaly" not in _types(lic.events)


# --------------------------------------------------------------------------
# AC8: rolled-back clock -> read-only + license.clock_anomaly
# --------------------------------------------------------------------------
def test_startup_with_rolled_back_clock(lic):
    # Stored last_seen is well ahead of the (rolled-back) "today".
    last_seen = datetime.date(2026, 6, 19)
    rolled_back_today = last_seen - datetime.timedelta(days=10)
    lic.store[lr.LICENSE_LAST_SEEN_KV] = last_seen.isoformat()
    lic.store[lr.LICENSE_KEY_KV] = lic.mint(
        expires_at=(rolled_back_today + datetime.timedelta(days=365)).isoformat()
    )

    result = lr.evaluate_license(today=rolled_back_today, public_key=lic.pub)

    assert result["status"] == LicenseStatus.READONLY
    assert result["reason"] == "clock_rollback"
    assert "license.clock_anomaly" in _types(lic.events)
    # last_seen must NOT be advanced while the clock is inconsistent.
    assert lic.store[lr.LICENSE_LAST_SEEN_KV] == last_seen.isoformat()


def test_clock_change_within_tolerance_does_not_trip(lic):
    last_seen = datetime.date(2026, 6, 19)
    today = last_seen - datetime.timedelta(days=1)  # within the 2-day window
    lic.store[lr.LICENSE_LAST_SEEN_KV] = last_seen.isoformat()
    lic.store[lr.LICENSE_KEY_KV] = lic.mint(
        expires_at=(today + datetime.timedelta(days=365)).isoformat()
    )

    result = lr.evaluate_license(today=today, public_key=lic.pub)

    assert result["status"] == LicenseStatus.VALID
    assert "license.clock_anomaly" not in _types(lic.events)
    # baseline advanced to today (clock considered consistent).
    assert lic.store[lr.LICENSE_LAST_SEEN_KV] == today.isoformat()


# --------------------------------------------------------------------------
# Expiry transitions -> grace / read-only + transition telemetry (AC11 chain)
# --------------------------------------------------------------------------
def test_grace_status_and_transition_event(lic):
    today = datetime.date.today()
    lic.store[lr.LICENSE_KEY_KV] = lic.mint(
        expires_at=(today - datetime.timedelta(days=5)).isoformat(), grace_days=14
    )
    lic.store[lr.LICENSE_LAST_STATUS_KV] = LicenseStatus.VALID  # prior state

    result = lr.evaluate_license(public_key=lic.pub)

    assert result["status"] == LicenseStatus.GRACE
    assert "license.entered_grace" in _types(lic.events)
    assert lic.store[lr.LICENSE_LAST_STATUS_KV] == LicenseStatus.GRACE


def test_readonly_after_grace_and_transition_event(lic):
    today = datetime.date.today()
    lic.store[lr.LICENSE_KEY_KV] = lic.mint(
        expires_at=(today - datetime.timedelta(days=30)).isoformat(), grace_days=14
    )
    lic.store[lr.LICENSE_LAST_STATUS_KV] = LicenseStatus.GRACE  # prior state

    result = lr.evaluate_license(public_key=lic.pub)

    assert result["status"] == LicenseStatus.READONLY
    assert "license.entered_readonly" in _types(lic.events)


def test_no_duplicate_transition_event_when_status_unchanged(lic):
    today = datetime.date.today()
    lic.store[lr.LICENSE_KEY_KV] = lic.mint(
        expires_at=(today - datetime.timedelta(days=5)).isoformat(), grace_days=14
    )
    lic.store[lr.LICENSE_LAST_STATUS_KV] = LicenseStatus.GRACE  # already in grace

    lr.evaluate_license(public_key=lic.pub)

    assert "license.entered_grace" not in _types(lic.events)
    assert "license.validated" in _types(lic.events)  # per-check event still fires


# --------------------------------------------------------------------------
# LICENSE_KEY env install path persists the key to the DB
# --------------------------------------------------------------------------
def test_env_license_key_is_persisted(lic):
    today = datetime.date.today()
    key = lic.mint(expires_at=(today + datetime.timedelta(days=100)).isoformat())
    lic.monkeypatch.setenv("LICENSE_KEY", key)

    result = lr.evaluate_license(public_key=lic.pub)

    assert result["status"] == LicenseStatus.VALID
    assert lic.store[lr.LICENSE_KEY_KV] == key  # persisted into the app DB


# --------------------------------------------------------------------------
# Side-effect-free read for the gate / status route
# --------------------------------------------------------------------------
def test_get_current_license_status_has_no_side_effects(lic):
    today = datetime.date.today()
    lic.store[lr.LICENSE_KEY_KV] = lic.mint(
        expires_at=(today + datetime.timedelta(days=10)).isoformat()
    )

    result = lr.get_current_license_status(public_key=lic.pub)

    assert result["status"] == LicenseStatus.VALID
    assert lr.LICENSE_LAST_SEEN_KV not in lic.store   # no persistence
    assert lic.events == []                           # no telemetry


# --------------------------------------------------------------------------
# Startup hook never raises
# --------------------------------------------------------------------------
def test_run_startup_validation_never_raises(lic, monkeypatch):
    def _boom(**_kw):
        raise RuntimeError("kv exploded")

    monkeypatch.setattr(lr, "evaluate_license", _boom)
    # Must swallow and return None rather than break app startup.
    assert lr.run_startup_validation() is None


# --------------------------------------------------------------------------
# Periodic scheduler wiring
# --------------------------------------------------------------------------
def test_scheduler_registers_job_and_is_idempotent():
    try:
        sched = lr.start_license_scheduler()
        assert sched.running
        assert sched.get_job(lr.LICENSE_JOB_ID) is not None
        # idempotent — a second call returns the same running scheduler.
        assert lr.start_license_scheduler() is sched
    finally:
        lr.stop_license_scheduler()
    assert not lr.scheduler.running
