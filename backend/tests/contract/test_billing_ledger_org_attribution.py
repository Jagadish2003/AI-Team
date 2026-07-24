"""R-1.9.1-L2 / T2 (AT-694) follow-up — billing ledger org attribution (AC2/AC4).

Regression guard for a cross-tenant misattribution the original T2 emit could not
avoid on the OAuth authorization_code path:

``telemetry.record_event`` attributes an event's stored org via
``resolve_event_org_id``, which lets the ambient REQUEST tenancy context WIN over
the payload ``org_id`` (by design — a stale explicit org must never misattribute an
in-request event). But the OAuth callback runs UNAUTHENTICATED: TenancyMiddleware
sets the ambient org to ``DEV_DEFAULT_ORG`` ("default"), while the callback connects
the org carried in its signed state nonce and calls ``emit_system_connected`` for
THAT org. Without pinning, the connect event is filed under "default" while its
``seq`` is drawn from the real org's counter — so the real tenant's usage report
misses the connect (AC2) AND its tamper chain shows a phantom seq gap (AC4 false
alarm), while "default" accumulates other tenants' phantom connects.

The fix pins event attribution to the transition org via
``tenancy.event_org_context`` for the duration of the emit. These tests prove the
pin holds even when the ambient context is a DIFFERENT org, and that it is a no-op
when the ambient org already matches. Pure unit tests — the telemetry write and the
seq counter are stubbed, and ``system_identity`` is passed so no DB/vault read runs.
"""
from __future__ import annotations

import pytest

from app import billing_ledger as bl
from app.middleware import tenancy


def _capture(monkeypatch):
    captured: dict = {}

    def _rec(event_type, payload=None):
        captured["event_type"] = event_type
        captured["payload"] = payload or {}
        # The ambient org record_event sees IS what resolve_event_org_id files under.
        captured["ctx_org"] = tenancy._current_org_id.get()

    monkeypatch.setattr("app.billing_ledger.record_event", _rec)
    monkeypatch.setattr("app.billing_chain.next_seq", lambda org: 1)
    return captured


def test_connect_pinned_to_transition_org_when_ambient_is_default(monkeypatch):
    """The OAuth-callback scenario: ambient context is "default", the connect is for
    "acme". The event must be filed under "acme"."""
    captured = _capture(monkeypatch)

    token = tenancy._current_org_id.set("default")  # unauthenticated callback ambient
    try:
        bl.emit_system_connected(
            "acme", "salesforce", was_connected=False,
            system_identity="https://acme.my.salesforce.com",
        )
        # The ambient context is untouched OUTSIDE the emit ...
        assert tenancy._current_org_id.get() == "default"
    finally:
        tenancy._current_org_id.reset(token)

    # ... but DURING the emit it was pinned to the transition org, so record_event's
    # context-wins attribution files the event under "acme" — matching its seq.
    assert captured["event_type"] == "billing.system_connected"
    assert captured["ctx_org"] == "acme"
    assert captured["payload"]["org_id"] == "acme"


def test_disconnect_pinned_to_transition_org_when_ambient_differs(monkeypatch):
    captured = _capture(monkeypatch)

    token = tenancy._current_org_id.set("default")
    try:
        bl.emit_system_disconnected(
            "acme", "jira", was_connected=True, system_identity="https://acme.atlassian.net",
        )
    finally:
        tenancy._current_org_id.reset(token)

    assert captured["event_type"] == "billing.system_disconnected"
    assert captured["ctx_org"] == "acme"
    assert captured["payload"]["org_id"] == "acme"


def test_pin_is_noop_when_ambient_already_matches(monkeypatch):
    """The authenticated connect paths (POST /connect, static/JWT/client-credentials)
    already run under the correct org — pinning must be a no-op there and restore it."""
    captured = _capture(monkeypatch)

    token = tenancy._current_org_id.set("acme")
    try:
        bl.emit_system_connected("acme", "slack", was_connected=False, system_identity="slack")
        assert tenancy._current_org_id.get() == "acme"
    finally:
        tenancy._current_org_id.reset(token)

    assert captured["ctx_org"] == "acme"
    assert captured["payload"]["org_id"] == "acme"


def test_record_connection_change_transition_is_pinned(monkeypatch):
    captured = _capture(monkeypatch)
    token = tenancy._current_org_id.set("default")
    try:
        bl.record_connection_change(
            "acme", "servicenow", was_connected=False, now_connected=True,
            system_identity="https://acme.service-now.com",
        )
    finally:
        tenancy._current_org_id.reset(token)
    assert captured["event_type"] == "billing.system_connected"
    assert captured["ctx_org"] == "acme"
    assert captured["payload"]["org_id"] == "acme"


def test_context_restored_even_if_record_event_raises(monkeypatch):
    """A telemetry failure inside the pinned block must not leak the pinned org into
    the surrounding request context (the ledger stays fire-and-forget)."""
    def _boom(*a, **k):
        raise RuntimeError("telemetry down")

    monkeypatch.setattr("app.billing_ledger.record_event", _boom)
    monkeypatch.setattr("app.billing_chain.next_seq", lambda org: 1)

    token = tenancy._current_org_id.set("default")
    try:
        # Must not raise (fire-and-forget) ...
        bl.emit_system_connected("acme", "salesforce", was_connected=False, system_identity="x")
        # ... and the ambient context must be restored to "default", not left as "acme".
        assert tenancy._current_org_id.get() == "default"
    finally:
        tenancy._current_org_id.reset(token)
