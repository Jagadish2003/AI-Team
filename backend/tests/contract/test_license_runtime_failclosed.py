"""Unit tests — LIC-1 review fix: periodic check pins read-only on failure.

`run_license_check()` must never raise, and when an org's evaluation fails it
must pin THAT org's cached status (org_licenses.last_status) to read-only so any
cache consumer stays conservative. Enforcement itself is live (the gate
re-validates), but the cache must not be left advertising a stale "valid".
Hermetic — the per-org storage layer is monkeypatched, no DB.
"""
from __future__ import annotations

from app import license_runtime as lr
from app.licensing import LicenseStatus


def test_run_license_check_pins_readonly_on_failure(monkeypatch):
    pins: list = []
    monkeypatch.setattr(lr, "all_licensed_org_ids", lambda: ["org-A"])
    monkeypatch.setattr(
        lr, "persist_org_status", lambda org, seen, status: pins.append((org, seen, status))
    )

    def _boom(*_a, **_k):
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(lr, "evaluate_license", _boom)

    lr.run_license_check()  # must not raise

    assert ("org-A", None, LicenseStatus.READONLY) in pins


def test_run_license_check_success_does_not_pin_readonly(monkeypatch):
    calls = {"ran": 0}
    pins: list = []
    monkeypatch.setattr(lr, "all_licensed_org_ids", lambda: ["org-A"])
    monkeypatch.setattr(
        lr, "persist_org_status", lambda org, seen, status: pins.append((org, seen, status))
    )

    def _ok(*_a, **_k):
        calls["ran"] += 1
        return {"status": LicenseStatus.VALID}

    monkeypatch.setattr(lr, "evaluate_license", _ok)

    lr.run_license_check()

    assert calls["ran"] == 1
    # A successful evaluation must not trigger a read-only override pin.
    assert pins == []


def test_run_license_check_swallows_storage_failure(monkeypatch):
    monkeypatch.setattr(lr, "all_licensed_org_ids", lambda: ["org-A"])

    def _boom(*_a, **_k):
        raise RuntimeError("evaluate failed")

    def _pin_boom(*_a, **_k):
        raise RuntimeError("pin also failed")

    monkeypatch.setattr(lr, "evaluate_license", _boom)
    monkeypatch.setattr(lr, "persist_org_status", _pin_boom)

    # Even if pinning the conservative status also fails, the job must not raise.
    lr.run_license_check()


def test_run_license_check_swallows_listing_failure(monkeypatch):
    def _boom(*_a, **_k):
        raise RuntimeError("cannot list orgs")

    monkeypatch.setattr(lr, "all_licensed_org_ids", _boom)

    # A failure to even list licensed orgs must not crash the scheduled job.
    lr.run_license_check()
