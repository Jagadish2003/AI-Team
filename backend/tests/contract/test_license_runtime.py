"""Unit tests for LIC-1 / T4 (AT-345) — startup/periodic license validation.

Covers the AC-mapped behaviours of ``app.license_runtime`` (now per-org):
  * startup with a valid key            -> status 'valid', last_seen persisted
  * startup with no key                 -> read-only 'no_license' (AC6)
  * startup with a rolled-back clock    -> read-only 'clock_rollback' + telemetry (AC8)
  * grace / read-only expiry transitions emit transition telemetry (AC11 chain)
  * the LICENSE_KEY env var is IGNORED (licensing is per-tenant, DB-sourced)
  * clock change within the 2-day tolerance does NOT trip the guard
  * the side-effect-free read used by the gate/status route

No network, no DB, no real keypair: the per-org storage layer is monkeypatched
to an in-memory dict keyed by org_id, telemetry is captured, and a throwaway
Ed25519 keypair signs the test keys (the matching public key is passed through to
validate_license).
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
    """In-memory per-org store + captured telemetry + a throwaway keypair.

    Returns an object with: ``org`` (the test org id), ``rows`` (org_id -> record
    dict), ``events`` (list of (type, payload)), ``priv``/``pub`` (Ed25519
    keypair), ``mint(expires_at, ...)``, plus install/set/get helpers operating on
    the test org by default.
    """
    ORG = "default"
    rows: dict = {}
    events: list = []

    def _ensure_row(org=ORG) -> dict:
        return rows.setdefault(
            org, {"license_key": None, "last_seen_date": None, "last_status": None}
        )

    def _read(org_id):
        r = rows.get(org_id)
        return dict(r) if r else None

    def _set_key(org_id, key):
        _ensure_row(org_id)["license_key"] = key

    def _persist_status(org_id, last_seen, last_status):
        # UPDATE-only: a keyless (row-less) org persists nothing.
        r = rows.get(org_id)
        if r is None:
            return
        if last_seen is not None:
            r["last_seen_date"] = last_seen
        r["last_status"] = last_status

    def _all():
        return [oid for oid, r in rows.items() if r.get("license_key")]

    monkeypatch.setattr(lr, "read_org_license", _read)
    monkeypatch.setattr(lr, "set_org_license_key", _set_key)
    monkeypatch.setattr(lr, "persist_org_status", _persist_status)
    monkeypatch.setattr(lr, "all_licensed_org_ids", _all)
    monkeypatch.setattr(lr, "record_event", lambda etype, payload=None: events.append((etype, payload or {})))
    monkeypatch.delenv("LICENSE_KEY", raising=False)

    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key()

    class _Ctx:
        pass

    ctx = _Ctx()
    ctx.org = ORG
    ctx.rows = rows
    ctx.events = events
    ctx.priv = priv
    ctx.pub = pub
    ctx.monkeypatch = monkeypatch
    ctx.mint = lambda **kw: _mint_key(priv, **kw)
    ctx.install = lambda key, org=ORG: _ensure_row(org).__setitem__("license_key", key)
    ctx.set_last_seen = lambda d, org=ORG: _ensure_row(org).__setitem__("last_seen_date", d)
    ctx.set_last_status = lambda s, org=ORG: _ensure_row(org).__setitem__("last_status", s)
    ctx.get_last_seen = lambda org=ORG: (rows.get(org) or {}).get("last_seen_date")
    ctx.get_last_status = lambda org=ORG: (rows.get(org) or {}).get("last_status")
    ctx.has_row = lambda org=ORG: org in rows
    return ctx


def _types(events):
    return [e[0] for e in events]


# --------------------------------------------------------------------------
# AC: startup with a valid key
# --------------------------------------------------------------------------
def test_startup_with_valid_key(lic):
    today = datetime.date.today()
    lic.install(lic.mint(expires_at=(today + datetime.timedelta(days=365)).isoformat()))

    result = lr.evaluate_license(org_id=lic.org, public_key=lic.pub)

    assert result["status"] == LicenseStatus.VALID
    assert result["customer"] == "City National Bank"
    # last_seen persisted as today on a clock-consistent pass.
    assert lic.get_last_seen() == today.isoformat()
    # per-check telemetry emitted for a verified key.
    assert "license.validated" in _types(lic.events)


# --------------------------------------------------------------------------
# AC6: startup with no key -> read-only "no valid license"
# --------------------------------------------------------------------------
def test_startup_with_no_key_is_readonly(lic):
    result = lr.evaluate_license(org_id=lic.org, public_key=lic.pub)

    assert result["status"] == LicenseStatus.READONLY
    assert result["reason"] == "no_license"
    # nothing to report about a non-existent customer.
    assert "license.validated" not in _types(lic.events)
    assert "license.clock_anomaly" not in _types(lic.events)
    # a keyless org stays row-less (nothing persisted).
    assert not lic.has_row()


# --------------------------------------------------------------------------
# AC8: rolled-back clock -> read-only + license.clock_anomaly
# --------------------------------------------------------------------------
def test_startup_with_rolled_back_clock(lic):
    # Stored last_seen is well ahead of the (rolled-back) "today".
    last_seen = datetime.date(2026, 6, 19)
    rolled_back_today = last_seen - datetime.timedelta(days=10)
    lic.set_last_seen(last_seen.isoformat())
    lic.install(lic.mint(expires_at=(rolled_back_today + datetime.timedelta(days=365)).isoformat()))

    result = lr.evaluate_license(org_id=lic.org, today=rolled_back_today, public_key=lic.pub)

    assert result["status"] == LicenseStatus.READONLY
    assert result["reason"] == "clock_rollback"
    assert "license.clock_anomaly" in _types(lic.events)
    # last_seen must NOT be advanced while the clock is inconsistent.
    assert lic.get_last_seen() == last_seen.isoformat()


def test_clock_change_within_tolerance_does_not_trip(lic):
    last_seen = datetime.date(2026, 6, 19)
    today = last_seen - datetime.timedelta(days=1)  # within the 2-day window
    lic.set_last_seen(last_seen.isoformat())
    lic.install(lic.mint(expires_at=(today + datetime.timedelta(days=365)).isoformat()))

    result = lr.evaluate_license(org_id=lic.org, today=today, public_key=lic.pub)

    assert result["status"] == LicenseStatus.VALID
    assert "license.clock_anomaly" not in _types(lic.events)
    # baseline advanced to today (clock considered consistent).
    assert lic.get_last_seen() == today.isoformat()


# --------------------------------------------------------------------------
# Expiry transitions -> grace / read-only + transition telemetry (AC11 chain)
# --------------------------------------------------------------------------
def test_grace_status_and_transition_event(lic):
    today = datetime.date.today()
    lic.install(lic.mint(
        expires_at=(today - datetime.timedelta(days=5)).isoformat(), grace_days=14
    ))
    lic.set_last_status(LicenseStatus.VALID)  # prior state

    result = lr.evaluate_license(org_id=lic.org, public_key=lic.pub)

    assert result["status"] == LicenseStatus.GRACE
    assert "license.entered_grace" in _types(lic.events)
    assert lic.get_last_status() == LicenseStatus.GRACE


def test_readonly_after_grace_and_transition_event(lic):
    today = datetime.date.today()
    lic.install(lic.mint(
        expires_at=(today - datetime.timedelta(days=30)).isoformat(), grace_days=14
    ))
    lic.set_last_status(LicenseStatus.GRACE)  # prior state

    result = lr.evaluate_license(org_id=lic.org, public_key=lic.pub)

    assert result["status"] == LicenseStatus.READONLY
    assert "license.entered_readonly" in _types(lic.events)


def test_no_duplicate_transition_event_when_status_unchanged(lic):
    today = datetime.date.today()
    lic.install(lic.mint(
        expires_at=(today - datetime.timedelta(days=5)).isoformat(), grace_days=14
    ))
    lic.set_last_status(LicenseStatus.GRACE)  # already in grace

    lr.evaluate_license(org_id=lic.org, public_key=lic.pub)

    assert "license.entered_grace" not in _types(lic.events)
    assert "license.validated" in _types(lic.events)  # per-check event still fires


# --------------------------------------------------------------------------
# The LICENSE_KEY env var is IGNORED — licensing is per-tenant, DB-sourced.
# --------------------------------------------------------------------------
def test_env_license_key_is_ignored(lic):
    today = datetime.date.today()
    key = lic.mint(expires_at=(today + datetime.timedelta(days=100)).isoformat())
    lic.monkeypatch.setenv("LICENSE_KEY", key)

    result = lr.evaluate_license(org_id=lic.org, public_key=lic.pub)

    # The env var is not consulted: a keyless org is still no_license, and nothing
    # is persisted from the environment.
    assert result["status"] == LicenseStatus.READONLY
    assert result["reason"] == "no_license"
    assert not lic.has_row()


# --------------------------------------------------------------------------
# Side-effect-free read for the gate / status route
# --------------------------------------------------------------------------
def test_get_current_license_status_has_no_side_effects(lic):
    today = datetime.date.today()
    lic.install(lic.mint(
        expires_at=(today + datetime.timedelta(days=10)).isoformat()
    ))

    result = lr.get_current_license_status(org_id=lic.org, public_key=lic.pub)

    assert result["status"] == LicenseStatus.VALID
    assert lic.get_last_seen() is None   # no persistence
    assert lic.events == []              # no telemetry


# --------------------------------------------------------------------------
# Startup hook never raises
# --------------------------------------------------------------------------
def test_run_startup_validation_never_raises(lic, monkeypatch):
    # A licensed org so the per-org loop actually runs (and hits the boom).
    lic.install(lic.mint(expires_at=datetime.date.today().isoformat()))

    def _boom(**_kw):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(lr, "evaluate_license", _boom)
    # Must swallow and return None rather than break app startup.
    assert lr.run_startup_validation() is None
