"""Unit tests — LIC-1 review fix: periodic check pins read-only on failure.

`run_license_check()` must never raise, and on a failed evaluation it must pin
the cached status signal (license:last_status) to read-only so any cache
consumer stays conservative. Enforcement itself is live (the gate re-validates),
but the cache must not be left advertising a stale "valid". Hermetic — kv is
monkeypatched, no DB.
"""
from __future__ import annotations

from app import license_runtime as lr
from app.licensing import LicenseStatus


def test_run_license_check_pins_readonly_on_failure(monkeypatch):
    store: dict = {}
    monkeypatch.setattr(lr, "kv_set", lambda k, v: store.__setitem__(k, v))

    def _boom(*_a, **_k):
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(lr, "evaluate_license", _boom)

    lr.run_license_check()  # must not raise

    assert store.get(lr.LICENSE_LAST_STATUS_KV) == LicenseStatus.READONLY


def test_run_license_check_success_does_not_pin_readonly(monkeypatch):
    calls = {"ran": False}

    def _ok(*_a, **_k):
        calls["ran"] = True
        return {"status": LicenseStatus.VALID}

    monkeypatch.setattr(lr, "evaluate_license", _ok)
    # If evaluate_license succeeds, run_license_check must not write a read-only
    # override on top of it.
    monkeypatch.setattr(lr, "kv_set", lambda *_a, **_k: None)

    lr.run_license_check()

    assert calls["ran"] is True


def test_run_license_check_swallows_kv_failure(monkeypatch):
    def _boom(*_a, **_k):
        raise RuntimeError("evaluate failed")

    def _kv_boom(*_a, **_k):
        raise RuntimeError("kv also failed")

    monkeypatch.setattr(lr, "evaluate_license", _boom)
    monkeypatch.setattr(lr, "kv_set", _kv_boom)

    # Even if pinning the conservative status also fails, the job must not raise.
    lr.run_license_check()
