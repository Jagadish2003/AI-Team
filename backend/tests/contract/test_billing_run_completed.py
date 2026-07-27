"""R-1.9.1-L2 / T1 (AT-693) — billing.run_completed emission (AC1).

Every discovery run emits ONE ``billing.run_completed`` event into the immutable
telemetry store, in every AI mode (hosted / in_boundary / customer_tenant),
carrying {run_id, org_id, ai_mode, provider, connected_system_count, pack_ids,
deployment_type, started_at, completed_at}. Billability is DERIVED BY THE L2
report, never decided at emission — so the event carries no billed/billable flag.

Two layers:
  * Unit tests of the emit helper + the ai-mode resolver (no DB): the payload
    shape, per-mode ai_mode, defensive fallbacks, and fire-and-forget resilience.
  * A contract test that drives a REAL offline discovery run through
    ``discovery.runner.run`` and asserts the event is emitted with the run's mode
    (this exercises the full run path and needs the contract Postgres — CI).
"""
from __future__ import annotations

import uuid

import pytest


# ---------------------------------------------------------------------------
# Registration — record_event() raises for an unregistered type, so the event
# type MUST be registered before any emission (telemetry contract).
# ---------------------------------------------------------------------------
def test_billing_run_completed_is_registered():
    from app.telemetry import REGISTERED_EVENT_TYPES

    assert "billing.run_completed" in REGISTERED_EVENT_TYPES


# ---------------------------------------------------------------------------
# The ai-mode resolver — reports the configured generation provider mode, with a
# defensive env fallback, and never raises.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("mode", ["hosted", "in_boundary", "customer_tenant"])
def test_resolve_ai_mode_reports_configured_provider(monkeypatch, mode):
    from discovery import runner

    monkeypatch.setenv("MODEL_GENERATION_PROVIDER", mode)
    ai_mode, provider = runner._resolve_ai_mode_and_provider()
    assert ai_mode == mode
    assert provider == mode


def test_resolve_ai_mode_falls_back_and_never_raises(monkeypatch):
    """An unregistered / misconfigured provider name never breaks metering — it
    falls back to the configured env value rather than raising."""
    from discovery import runner

    monkeypatch.setenv("MODEL_GENERATION_PROVIDER", "not_a_real_provider")
    ai_mode, provider = runner._resolve_ai_mode_and_provider()
    assert ai_mode == "not_a_real_provider"
    assert provider == "not_a_real_provider"


# ---------------------------------------------------------------------------
# The emit helper — full payload shape (AC1), per mode.
# ---------------------------------------------------------------------------
def _capture(monkeypatch):
    from discovery import runner

    events: list = []
    monkeypatch.setattr(runner, "record_event", lambda et, p=None: events.append((et, p or {})))
    return events


def test_emit_billing_run_completed_full_shape(monkeypatch):
    from discovery import runner

    monkeypatch.setenv("MODEL_GENERATION_PROVIDER", "hosted")
    monkeypatch.setattr("app.license_limits.count_connected_systems", lambda org: 3)
    events = _capture(monkeypatch)

    runner._emit_billing_run_completed(
        org_id="org-A",
        run_id="run-1",
        pack_id="service_cloud",
        deployment_type="saas",
        started_at="2026-01-01T00:00:00+00:00",
    )

    billing = [p for et, p in events if et == "billing.run_completed"]
    assert len(billing) == 1
    p = billing[0]
    # Every field the doc's event schema requires is present and correct.
    assert p["run_id"] == "run-1"
    assert p["org_id"] == "org-A"
    assert p["ai_mode"] == "hosted"
    assert p["provider"] == "hosted"
    assert p["connected_system_count"] == 3
    assert p["pack_ids"] == ["service_cloud"]
    assert p["deployment_type"] == "saas"
    assert p["started_at"] == "2026-01-01T00:00:00+00:00"
    assert isinstance(p["completed_at"], str) and p["completed_at"]
    # Billability is DERIVED BY THE REPORT, not decided at emission — the event
    # carries no billed/billable verdict.
    assert "billable" not in p and "billed" not in p


@pytest.mark.parametrize("mode", ["hosted", "in_boundary", "customer_tenant"])
def test_emit_reports_each_ai_mode(monkeypatch, mode):
    """AC1: hosted emits mode 'hosted'; in-boundary / customer-tenant emit the
    same event with their mode — all into the immutable store."""
    from discovery import runner

    monkeypatch.setenv("MODEL_GENERATION_PROVIDER", mode)
    monkeypatch.setattr("app.license_limits.count_connected_systems", lambda org: 0)
    events = _capture(monkeypatch)

    runner._emit_billing_run_completed(
        org_id="o", run_id="r", pack_id="pk", deployment_type=None, started_at="t0"
    )

    p = [pl for et, pl in events if et == "billing.run_completed"][0]
    assert p["ai_mode"] == mode
    assert p["provider"] == mode


def test_pack_ids_is_a_list(monkeypatch):
    """pack_ids is emitted as a list (forward-compatible with multi-pack runs),
    even for today's single-pack run."""
    from discovery import runner

    monkeypatch.setenv("MODEL_GENERATION_PROVIDER", "hosted")
    monkeypatch.setattr("app.license_limits.count_connected_systems", lambda org: 1)
    events = _capture(monkeypatch)

    runner._emit_billing_run_completed(
        org_id="o", run_id="r", pack_id="ncino", deployment_type=None, started_at="t0"
    )
    p = [pl for et, pl in events if et == "billing.run_completed"][0]
    assert p["pack_ids"] == ["ncino"]


def test_connected_system_count_defensive_on_error(monkeypatch):
    """A failure counting connected systems must not break the emit — the event
    is still emitted with connected_system_count=None."""
    from discovery import runner

    monkeypatch.setenv("MODEL_GENERATION_PROVIDER", "hosted")

    def _boom(org):
        raise RuntimeError("db down")

    monkeypatch.setattr("app.license_limits.count_connected_systems", _boom)
    events = _capture(monkeypatch)

    runner._emit_billing_run_completed(
        org_id="o", run_id="r", pack_id="pk", deployment_type=None, started_at="t0"
    )
    p = [pl for et, pl in events if et == "billing.run_completed"][0]
    assert p["connected_system_count"] is None


def test_emit_is_fire_and_forget(monkeypatch):
    """A telemetry write failure must never propagate out of the emit helper —
    metering can never break or fail a discovery run."""
    from discovery import runner

    monkeypatch.setenv("MODEL_GENERATION_PROVIDER", "hosted")
    monkeypatch.setattr("app.license_limits.count_connected_systems", lambda org: 0)

    def _boom(*a, **k):
        raise RuntimeError("telemetry store unavailable")

    monkeypatch.setattr(runner, "record_event", _boom)
    # Must not raise.
    runner._emit_billing_run_completed(
        org_id="o", run_id="r", pack_id="pk", deployment_type=None, started_at="t0"
    )


# ---------------------------------------------------------------------------
# End-to-end: a real offline run emits billing.run_completed with its AI mode.
# Drives discovery.runner.run through the full pipeline (needs the contract DB).
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("mode", ["hosted", "in_boundary", "customer_tenant"])
def test_offline_run_emits_billing_run_completed(monkeypatch, mode):
    from discovery import runner

    monkeypatch.setenv("MODEL_GENERATION_PROVIDER", mode)
    events: list = []
    monkeypatch.setattr(runner, "record_event", lambda et, p=None: events.append((et, p or {})))

    runner.run(
        mode="offline",
        org_id=f"org_bill_{uuid.uuid4().hex[:8]}",
        run_id=f"run_{uuid.uuid4().hex[:8]}",
    )

    billing = [p for et, p in events if et == "billing.run_completed"]
    assert billing, "every run must emit exactly one billing.run_completed"
    p = billing[-1]
    assert p["ai_mode"] == mode
    assert isinstance(p["pack_ids"], list) and p["pack_ids"]
    assert (p["connected_system_count"] is None) or isinstance(p["connected_system_count"], int)
    assert p["run_id"] and p["org_id"]
    assert p["started_at"] and p["completed_at"]
