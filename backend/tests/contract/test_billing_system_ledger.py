"""R-1.9.1-L2 / T2 (AT-694) — billing.system_connected / system_disconnected (AC2).

A system connect/disconnect ledger backs the L2 usage report's pro-ration record
for mid-term system additions/removals. On each GENUINE Integration-Hub connect a
``billing.system_connected`` event is emitted, and on each genuine disconnect a
``billing.system_disconnected``, each carrying {connector, system_identity,
occurred_at} (plus org_id + the T4 tamper-evidence seq).

Layers:
  * Registration — record_event() raises for an unregistered type, so both event
    types must be registered before emission.
  * The emit helpers (DB-free, record_event captured): full payload shape,
    transition-gating (re-auth / no-op disconnect emit nothing), identity
    resolution + fallback, seq stamping, and fire-and-forget resilience.
  * A cross-check that the emitted payload is exactly what the T3 usage report
    (``app.usage_report``) aggregates into its system_ledger — the shape contract
    between this task and its consumer.
"""
from __future__ import annotations

import json

import pytest

import app.billing_ledger as bl


# ---------------------------------------------------------------------------
# Registration — telemetry contract.
# ---------------------------------------------------------------------------
def test_ledger_event_types_are_registered():
    from app.telemetry import REGISTERED_EVENT_TYPES

    assert "billing.system_connected" in REGISTERED_EVENT_TYPES
    assert "billing.system_disconnected" in REGISTERED_EVENT_TYPES


# ---------------------------------------------------------------------------
# Capture helper — replace record_event so the emitters are DB-free.
# ---------------------------------------------------------------------------
def _capture(monkeypatch, *, seq=None):
    events: list = []
    monkeypatch.setattr(bl, "record_event", lambda et, p=None: events.append((et, p or {})))
    # Stub the seq counter deterministically (billing_chain hits the DB otherwise).
    import app.billing_chain as bc

    monkeypatch.setattr(bc, "next_seq", lambda org: seq)
    # Identity resolves to the connector id unless a test overrides it.
    monkeypatch.setattr(bl, "resolve_system_identity", lambda org, cid: cid)
    return events


# ---------------------------------------------------------------------------
# Payload shape (AC2) — {connector, system_identity, occurred_at} + org_id + seq.
# ---------------------------------------------------------------------------
def test_connected_full_shape(monkeypatch):
    events = _capture(monkeypatch, seq=7)

    bl.emit_system_connected("org-A", "salesforce", was_connected=False)

    emitted = [p for et, p in events if et == "billing.system_connected"]
    assert len(emitted) == 1
    p = emitted[0]
    assert p["connector"] == "salesforce"
    assert p["system_identity"] == "salesforce"
    assert p["org_id"] == "org-A"
    assert p["seq"] == 7
    assert isinstance(p["occurred_at"], str) and p["occurred_at"]
    # No billability verdict is decided at emission — the report derives it (T3).
    assert "billable" not in p and "billed" not in p


def test_disconnected_full_shape(monkeypatch):
    events = _capture(monkeypatch, seq=8)

    bl.emit_system_disconnected(
        "org-A", "jira", was_connected=True, system_identity="https://acme.atlassian.net"
    )

    emitted = [p for et, p in events if et == "billing.system_disconnected"]
    assert len(emitted) == 1
    p = emitted[0]
    assert p["connector"] == "jira"
    # An explicit identity (resolved before the credential was revoked) wins.
    assert p["system_identity"] == "https://acme.atlassian.net"
    assert p["seq"] == 8
    assert isinstance(p["occurred_at"], str) and p["occurred_at"]


# ---------------------------------------------------------------------------
# Transition-gating — only genuine state changes ledger (no phantom pro-ration).
# ---------------------------------------------------------------------------
def test_reauth_of_connected_system_emits_nothing(monkeypatch):
    events = _capture(monkeypatch)
    bl.emit_system_connected("org-A", "salesforce", was_connected=True)
    assert events == []


def test_disconnect_of_never_connected_emits_nothing(monkeypatch):
    events = _capture(monkeypatch)
    bl.emit_system_disconnected("org-A", "salesforce", was_connected=False)
    assert events == []


def test_record_connection_change_both_directions(monkeypatch):
    events = _capture(monkeypatch)

    # not-connected -> connected == addition
    bl.record_connection_change("o", "slack", was_connected=False, now_connected=True)
    # connected -> not-connected == removal
    bl.record_connection_change("o", "slack", was_connected=True, now_connected=False)
    # no-op transitions emit nothing
    bl.record_connection_change("o", "slack", was_connected=True, now_connected=True)
    bl.record_connection_change("o", "slack", was_connected=False, now_connected=False)

    kinds = [et for et, _ in events]
    assert kinds == ["billing.system_connected", "billing.system_disconnected"]


# ---------------------------------------------------------------------------
# Identity resolution — instance URL first, static base_url next, connector id last.
# ---------------------------------------------------------------------------
def test_identity_prefers_instance_url(monkeypatch):
    monkeypatch.setattr(
        "app.live_ingest_credentials.get_connector_instance_url",
        lambda org, cid: "https://acme.my.salesforce.com",
    )
    assert (
        bl.resolve_system_identity("o", "salesforce") == "https://acme.my.salesforce.com"
    )


def test_identity_falls_back_to_static_base_url(monkeypatch):
    monkeypatch.setattr(
        "app.live_ingest_credentials.get_connector_instance_url", lambda org, cid: None
    )
    monkeypatch.setattr(
        "app.auth.vault.get_static_credential_metadata",
        lambda org, cid: {"base_url": "https://acme.service-now.com", "has_username": True},
    )
    assert bl.resolve_system_identity("o", "servicenow") == "https://acme.service-now.com"


def test_identity_falls_back_to_connector_id(monkeypatch):
    monkeypatch.setattr(
        "app.live_ingest_credentials.get_connector_instance_url", lambda org, cid: None
    )
    monkeypatch.setattr(
        "app.auth.vault.get_static_credential_metadata", lambda org, cid: None
    )
    # A connector with neither an instance URL nor static base_url (e.g. Slack).
    assert bl.resolve_system_identity("o", "slack") == "slack"


def test_identity_never_raises_on_lookup_error(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(
        "app.live_ingest_credentials.get_connector_instance_url", _boom
    )
    monkeypatch.setattr("app.auth.vault.get_static_credential_metadata", _boom)
    assert bl.resolve_system_identity("o", "github") == "github"


# ---------------------------------------------------------------------------
# seq is defensive, and emission is fire-and-forget.
# ---------------------------------------------------------------------------
def test_seq_is_none_when_counter_unavailable(monkeypatch):
    events = _capture(monkeypatch, seq=None)  # next_seq stub returns None
    bl.emit_system_connected("o", "teams", was_connected=False)
    p = [pl for et, pl in events if et == "billing.system_connected"][0]
    assert p["seq"] is None


def test_emit_is_fire_and_forget(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("telemetry store unavailable")

    monkeypatch.setattr(bl, "record_event", _boom)
    monkeypatch.setattr(bl, "resolve_system_identity", lambda org, cid: cid)
    # Must not raise — metering can never break a connect/disconnect request.
    bl.emit_system_connected("o", "teams", was_connected=False)
    bl.emit_system_disconnected("o", "teams", was_connected=True)


# ---------------------------------------------------------------------------
# Shape contract with the T3 consumer — the emitted payload IS what the usage
# report aggregates into system_ledger.
# ---------------------------------------------------------------------------
def test_emitted_payload_feeds_usage_report_ledger(monkeypatch):
    events = _capture(monkeypatch, seq=1)

    bl.emit_system_connected("org-A", "salesforce", was_connected=False)
    bl.emit_system_disconnected(
        "org-A", "jira", was_connected=True, system_identity="jira-1"
    )

    connected = [p for et, p in events if et == "billing.system_connected"]
    disconnected = [p for et, p in events if et == "billing.system_disconnected"]

    import app.usage_report as ur

    class _Ev:
        def __init__(self, payload):
            self.payload = json.dumps(payload)

    def _range(org_id, event_type, from_dt, to_dt, limit=10000):
        if event_type == ur.BILLING_SYSTEM_CONNECTED:
            return [_Ev(p) for p in connected]
        if event_type == ur.BILLING_SYSTEM_DISCONNECTED:
            return [_Ev(p) for p in disconnected]
        return []

    monkeypatch.setattr("app.usage_report.get_telemetry_range", _range)
    body = ur.build_usage_report_body(
        "org-A", "2026-07-01", "2026-07-31", kid="k", license_org_id="org-A", generated_at="t"
    )

    ledger = {(e["event"], e["connector"], e["system_identity"]) for e in body["system_ledger"]}
    assert ("connected", "salesforce", "salesforce") in ledger
    assert ("disconnected", "jira", "jira-1") in ledger
    assert body["event_count"] == 2


# ---------------------------------------------------------------------------
# is_connected reads the org connector state and is defensive.
# ---------------------------------------------------------------------------
def test_is_connected_reflects_connector_status(monkeypatch):
    monkeypatch.setattr(
        "app.db.org_connector_get", lambda org, cid: {"status": "connected"}
    )
    assert bl.is_connected("o", "salesforce") is True
    monkeypatch.setattr(
        "app.db.org_connector_get", lambda org, cid: {"status": "disconnected"}
    )
    assert bl.is_connected("o", "salesforce") is False
    monkeypatch.setattr("app.db.org_connector_get", lambda org, cid: None)
    assert bl.is_connected("o", "salesforce") is False


def test_is_connected_never_raises(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr("app.db.org_connector_get", _boom)
    assert bl.is_connected("o", "salesforce") is False
