"""2.0-B1 T6 — export-audit logging unit tests (DB-free).

AC6: "Every export generation is an audit event naming user, scope, and time."

The end-to-end HTTP + real-``audit_log`` assertions live in
``tests/contract/test_r2_0_b1_acceptance.py`` (they need PostgreSQL). This file
pins the parts that must not depend on a database being reachable:

  * the payload always names the acting user, the export scope, and an ISO-8601
    UTC time — and a caller cannot blank any of them out;
  * actor resolution is fail-safe (never raises) and never records a blank
    actor silently;
  * an unknown export kind fails loudly instead of writing an unclassifiable
    record, while a failed audit/telemetry WRITE never denies an already-signed
    artifact;
  * every export-generating route is a registered audit surface, so a new export
    endpoint cannot ship unaudited by omission;
  * the payload carries identifiers/counts/hashes only — never a whole signature.
"""
from __future__ import annotations

import datetime as dt

import pytest

from app import export_audit as ea


# ── payload shape: user, scope, time (AC6) ──────────────────────────────────


def test_payload_names_user_scope_and_time():
    payload = ea.build_export_audit_payload(
        ea.EXPORT_KIND_EVIDENCE_FINDING,
        actor="user-42",
        scope="finding",
        details={"run_id": "run_1", "opportunity_id": "opp_1"},
    )
    assert payload["user_id"] == "user-42"
    assert payload["scope"] == "finding"
    assert payload["export_kind"] == ea.EXPORT_KIND_EVIDENCE_FINDING
    # An ISO-8601 UTC instant, parseable by a third party.
    parsed = dt.datetime.fromisoformat(payload["timestamp"])
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == dt.timedelta(0)
    # The fingerprint identifying WHAT was exported rides along.
    assert payload["run_id"] == "run_1"
    assert payload["opportunity_id"] == "opp_1"


def test_scope_defaults_per_kind_when_the_caller_supplies_none():
    for kind, expected in (
        (ea.EXPORT_KIND_EVIDENCE_FINDING, "finding"),
        (ea.EXPORT_KIND_EVIDENCE_REPORT, "report"),
        (ea.EXPORT_KIND_USAGE_REPORT, "usage_report"),
    ):
        payload = ea.build_export_audit_payload(kind, actor="u", scope=None)
        assert payload["scope"] == expected, kind


def test_blank_or_missing_actor_records_the_explicit_sentinel():
    """'We do not know who' must never look like 'no user was involved'."""
    for actor in (None, "", "   "):
        payload = ea.build_export_audit_payload(
            ea.EXPORT_KIND_EVIDENCE_REPORT, actor=actor
        )
        assert payload["user_id"] == ea.UNATTRIBUTED_ACTOR


def test_details_cannot_overwrite_the_mandatory_parts():
    """A stray fingerprint key must not be able to blank the actor or the time."""
    payload = ea.build_export_audit_payload(
        ea.EXPORT_KIND_EVIDENCE_FINDING,
        actor="user-42",
        scope="finding",
        details={"user_id": "someone-else", "timestamp": "not-a-time",
                 "export_kind": "spoofed", "run_id": "run_1"},
    )
    assert payload["user_id"] == "user-42"
    assert payload["timestamp"] != "not-a-time"
    assert payload["export_kind"] == ea.EXPORT_KIND_EVIDENCE_FINDING
    assert payload["run_id"] == "run_1"


def test_an_explicit_timestamp_is_honoured_for_reproducible_records():
    payload = ea.build_export_audit_payload(
        ea.EXPORT_KIND_USAGE_REPORT, actor="u", timestamp="2026-07-30T10:00:00+00:00"
    )
    assert payload["timestamp"] == "2026-07-30T10:00:00+00:00"


def test_unknown_export_kind_fails_loudly():
    """Every call site passes a module constant, so an unknown kind is a
    programming error — not something to file as an unclassifiable audit row."""
    with pytest.raises(ValueError) as exc:
        ea.build_export_audit_payload("pdf_maybe", actor="u")
    assert "unknown export kind" in str(exc.value)


# ── actor resolution ────────────────────────────────────────────────────────


def test_actor_resolves_through_the_shared_rbac_resolver(monkeypatch):
    monkeypatch.setattr("app.rbac._get_user_id_from_token", lambda t: f"uid::{t}")
    assert ea.resolve_export_actor("tok-1") == "uid::tok-1"


def test_actor_resolution_never_raises(monkeypatch):
    def _boom(_token):
        raise RuntimeError("token store down")

    monkeypatch.setattr("app.rbac._get_user_id_from_token", _boom)
    assert ea.resolve_export_actor("tok-1") == ea.UNATTRIBUTED_ACTOR


def test_missing_token_is_unattributed_not_empty():
    assert ea.resolve_export_actor(None) == ea.UNATTRIBUTED_ACTOR
    assert ea.resolve_export_actor("") == ea.UNATTRIBUTED_ACTOR


def test_resolver_returning_nothing_is_unattributed(monkeypatch):
    monkeypatch.setattr("app.rbac._get_user_id_from_token", lambda t: "")
    assert ea.resolve_export_actor("tok-1") == ea.UNATTRIBUTED_ACTOR


# ── recording ───────────────────────────────────────────────────────────────


@pytest.fixture
def recorded(monkeypatch):
    calls = {"audit": [], "telemetry": []}
    monkeypatch.setattr(
        "app.middleware.audit.log_event",
        lambda et, **kw: calls["audit"].append((et, kw)),
    )
    monkeypatch.setattr(
        "app.telemetry.record_event",
        lambda et, payload=None: calls["telemetry"].append((et, payload)),
    )
    return calls


def test_finding_export_writes_the_audit_event_with_the_actor(recorded):
    payload = ea.record_export_generated(
        ea.EXPORT_KIND_EVIDENCE_FINDING,
        org_id="org_a",
        actor="user-42",
        scope="finding",
        details={"run_id": "run_1", "content_root": "abc"},
    )
    assert len(recorded["audit"]) == 1
    event_type, kwargs = recorded["audit"][0]
    assert event_type == "evidence_export_generated"
    assert kwargs["org_id"] == "org_a"
    # log_event lifts user_id into the audit row's own actor column.
    assert kwargs["user_id"] == "user-42"
    assert kwargs["scope"] == "finding"
    assert kwargs["timestamp"]
    assert kwargs["run_id"] == "run_1"
    # The returned payload IS what was written (minus the org routing key), so a
    # caller/test can see exactly what the audit trail now holds.
    assert {k: v for k, v in kwargs.items() if k != "org_id"} == payload


def test_report_export_uses_the_same_audit_event_with_report_scope(recorded):
    ea.record_export_generated(
        ea.EXPORT_KIND_EVIDENCE_REPORT, org_id="org_a", actor="u", scope="report"
    )
    event_type, kwargs = recorded["audit"][0]
    assert event_type == "evidence_export_generated"
    assert kwargs["scope"] == "report"
    assert kwargs["export_kind"] == ea.EXPORT_KIND_EVIDENCE_REPORT


def test_usage_report_export_is_audited_under_its_own_event(recorded):
    ea.record_export_generated(
        ea.EXPORT_KIND_USAGE_REPORT,
        org_id="org_a",
        actor="owner-1",
        details={"period_from": "2026-07-01", "period_to": "2026-07-31"},
    )
    event_type, kwargs = recorded["audit"][0]
    assert event_type == "usage_report_exported"
    assert kwargs["user_id"] == "owner-1"
    assert kwargs["scope"] == "usage_report"
    assert kwargs["period_from"] == "2026-07-01"
    # No telemetry type is registered for this kind — the audit record is the
    # AC6 requirement and the billing ledger already carries the commercial trail.
    assert recorded["telemetry"] == []


def test_evidence_export_also_emits_telemetry_without_the_actor(recorded):
    """Telemetry is observability, not an audit trail: the actor stays out of it."""
    ea.record_export_generated(
        ea.EXPORT_KIND_EVIDENCE_FINDING,
        org_id="org_a",
        actor="user-42",
        scope="finding",
        details={"content_root": "abc"},
    )
    assert len(recorded["telemetry"]) == 1
    event_type, payload = recorded["telemetry"][0]
    assert event_type == "export.evidence_generated"
    assert payload["content_root"] == "abc"
    assert payload["scope"] == "finding"
    assert "user_id" not in payload


def test_a_recording_failure_never_propagates(monkeypatch):
    """The artifact is already signed and served; a broken audit store must not
    turn a successful export into a 500."""
    def _boom(*a, **k):
        raise RuntimeError("audit store down")

    monkeypatch.setattr("app.middleware.audit.log_event", _boom)
    monkeypatch.setattr("app.telemetry.record_event", _boom)
    payload = ea.record_export_generated(
        ea.EXPORT_KIND_EVIDENCE_FINDING, org_id="org_a", actor="u", scope="finding"
    )
    assert payload["user_id"] == "u"   # still reports what it tried to write


def test_recording_an_unknown_kind_raises_rather_than_dropping_the_event(recorded):
    with pytest.raises(ValueError):
        ea.record_export_generated("pdf_maybe", org_id="org_a", actor="u")
    assert recorded["audit"] == []


def test_no_whole_signature_reaches_the_audit_payload(recorded):
    signature = "a" * 64
    ea.record_export_generated(
        ea.EXPORT_KIND_EVIDENCE_FINDING,
        org_id="org_a",
        actor="u",
        scope="finding",
        details={"signature_prefix": signature[:16]},
    )
    _, kwargs = recorded["audit"][0]
    assert signature not in str(kwargs)
    assert kwargs["signature_prefix"] == signature[:16]


# ── registries: every export surface is audited ─────────────────────────────


def test_audit_event_types_are_registered():
    from app.middleware.audit import (
        AUDIT_EVENT_REGISTRY,
        EVIDENCE_EXPORT_GENERATED,
        USAGE_REPORT_EXPORTED,
    )

    assert EVIDENCE_EXPORT_GENERATED in AUDIT_EVENT_REGISTRY
    assert USAGE_REPORT_EXPORTED in AUDIT_EVENT_REGISTRY
    # Every kind this module can record must map to a registered audit type.
    for kind in ea.VALID_EXPORT_KINDS:
        assert ea._AUDIT_EVENT_BY_KIND[kind] in AUDIT_EVENT_REGISTRY


def test_mapped_telemetry_types_are_registered():
    """``record_event`` raises for an unregistered type, so a kind may only map
    to a telemetry event once its payload schema exists."""
    from app.telemetry import REGISTERED_EVENT_TYPES

    for kind in ea.VALID_EXPORT_KINDS:
        event = ea._TELEMETRY_EVENT_BY_KIND.get(kind)
        if event:
            assert event in REGISTERED_EVENT_TYPES, kind


def test_every_export_surface_maps_to_a_valid_kind():
    for path, kind in ea.EXPORT_AUDIT_SURFACES.items():
        assert kind in ea.VALID_EXPORT_KINDS, path


def test_the_known_export_routes_are_registered_surfaces():
    from app.routes_evidence_export import FINDING_EXPORT_PATH, REPORT_EXPORT_PATH
    from app.routes_usage_report import USAGE_REPORT_PATH

    for path in (FINDING_EXPORT_PATH, REPORT_EXPORT_PATH, USAGE_REPORT_PATH):
        assert path in ea.EXPORT_AUDIT_SURFACES, path


def test_export_routes_declare_the_bearer_token_so_the_actor_is_known():
    """A route that does not take the token cannot name the user (AC6). Pinning
    the signature stops a future refactor from quietly dropping attribution back
    to '_unattributed'."""
    import inspect

    from app.routes_evidence_export import (
        get_finding_evidence_export,
        get_report_evidence_export,
    )
    from app.routes_usage_report import get_usage_report

    for endpoint in (
        get_finding_evidence_export, get_report_evidence_export, get_usage_report
    ):
        assert "token" in inspect.signature(endpoint).parameters, endpoint.__name__
