"""2.0-B1 T4 — evidence-export ROUTE logic tests (DB-free).

The end-to-end HTTP tests live in
``tests/contract/test_evidence_export_contract.py`` (they need PostgreSQL). This
file pins the route module's own decisions, which are security-relevant and
must not depend on a database being reachable:

  * cross-org and unknown runs are indistinguishable 404s — a signed export must
    never attest to another tenant's data;
  * an :class:`EvidenceExportError` maps to 404 vs 400 correctly, so an unsigned
    bundle is never returned in place of an error;
  * the download form serves the canonical bytes a verifier checks, with a safe
    filename;
  * the audit/telemetry records carry the fingerprint only, and a recording
    failure never denies the caller their (already-signed) artifact.
"""
from __future__ import annotations

from typing import Any, Dict

import pytest
from fastapi import HTTPException

from app import evidence_export as ee
from app import routes_evidence_export as routes


# ── tenancy ─────────────────────────────────────────────────────────────────


def test_cross_org_run_is_a_404_not_a_signed_empty_bundle(monkeypatch):
    monkeypatch.setattr(routes.db, "run_get", lambda rid: {"id": rid, "org_id": "org_b"})
    with pytest.raises(HTTPException) as exc:
        routes._require_run_in_org("run_x", "org_a")
    assert exc.value.status_code == 404
    assert "not found" in str(exc.value.detail)


def test_same_org_run_is_returned(monkeypatch):
    monkeypatch.setattr(routes.db, "run_get", lambda rid: {"id": rid, "org_id": "org_a"})
    assert routes._require_run_in_org("run_x", "org_a")["id"] == "run_x"


def test_org_on_run_inputs_is_honoured(monkeypatch):
    monkeypatch.setattr(
        routes.db, "run_get", lambda rid: {"id": rid, "inputs": {"orgId": "org_b"}}
    )
    with pytest.raises(HTTPException) as exc:
        routes._require_run_in_org("run_x", "org_a")
    assert exc.value.status_code == 404


def test_legacy_untagged_run_is_not_filtered(monkeypatch):
    """A run created before org-tagging carries no org — it is not hidden."""
    monkeypatch.setattr(routes.db, "run_get", lambda rid: {"id": rid})
    assert routes._require_run_in_org("run_legacy", "org_a")["id"] == "run_legacy"


# ── error mapping — never an unsigned bundle ────────────────────────────────


def test_not_found_errors_map_to_404(monkeypatch):
    def _raise(*a, **k):
        raise ee.EvidenceExportError("opportunity 'opp_x' not found in run 'run_1'")

    monkeypatch.setattr(routes, "generate_signed_export", _raise)
    with pytest.raises(HTTPException) as exc:
        routes._generate("org_a", "run_1", scope=ee.SCOPE_FINDING, opp_id="opp_x")
    assert exc.value.status_code == 404


def test_unsignable_export_maps_to_400_with_the_reason(monkeypatch):
    def _raise(*a, **k):
        raise ee.EvidenceExportError("the installed license carries no report_key")

    monkeypatch.setattr(routes, "generate_signed_export", _raise)
    with pytest.raises(HTTPException) as exc:
        routes._generate("org_a", "run_1", scope=ee.SCOPE_FINDING, opp_id="opp_1")
    assert exc.value.status_code == 400
    assert "report_key" in str(exc.value.detail)


# ── download form ───────────────────────────────────────────────────────────


def _envelope() -> Dict[str, Any]:
    body = {
        "scope": "finding", "run_id": "run_1", "opportunity_id": "opp_001",
        "finding_count": 1, "generated_at": "2026-07-02T00:00:00+00:00",
        "integrity": {"record_count": 3, "content_root": "abc123"},
    }
    return {
        "bundle": body,
        "signature": ee.sign_report_body(body, "rk"),
        "algorithm": ee.SIGNATURE_ALGORITHM,
    }


def test_json_body_is_returned_by_default():
    envelope = _envelope()
    assert routes._serve(envelope, False) is envelope


def test_download_serves_canonical_bytes_that_verify():
    envelope = _envelope()
    response = routes._serve(envelope, True)
    assert response.media_type == "application/json"
    assert "attachment" in response.headers["content-disposition"]
    # The served bytes are exactly what a third-party verifier checks.
    assert ee.verify_export_bytes(response.body, "rk")["signature_valid"] is True


def test_download_filename_is_sanitised():
    envelope = _envelope()
    envelope["bundle"]["run_id"] = "run/../../etc/passwd"
    envelope["bundle"]["opportunity_id"] = 'opp"; rm -rf /'
    name = routes._filename(envelope)
    for bad in ('"', "/", "\\", ";", " "):
        assert bad not in name
    assert name.endswith(".json")


# ── audit / telemetry recording ─────────────────────────────────────────────


def test_export_is_audited_and_metered_with_fingerprint_only(monkeypatch):
    audit_calls: list = []
    telemetry_calls: list = []
    monkeypatch.setattr("app.middleware.audit.log_event", lambda et, **kw: audit_calls.append((et, kw)))
    monkeypatch.setattr("app.telemetry.record_event", lambda et, payload=None: telemetry_calls.append((et, payload)))

    envelope = _envelope()
    routes._record_export("org_a", envelope)

    assert len(audit_calls) == 1
    event_type, payload = audit_calls[0]
    assert event_type == "evidence_export_generated"
    assert payload["org_id"] == "org_a"
    assert payload["run_id"] == "run_1"
    assert payload["content_root"] == "abc123"
    assert payload["signature_prefix"] == envelope["signature"][:16]
    assert envelope["signature"] not in str(payload)   # never the whole MAC

    assert len(telemetry_calls) == 1
    assert telemetry_calls[0][0] == "export.evidence_generated"
    assert telemetry_calls[0][1]["content_root"] == "abc123"


def test_a_recording_failure_never_denies_the_signed_artifact(monkeypatch):
    """The bundle is already produced; failing to log it must not raise."""
    def _boom(*a, **k):
        raise RuntimeError("audit store down")

    monkeypatch.setattr("app.middleware.audit.log_event", _boom)
    monkeypatch.setattr("app.telemetry.record_event", _boom)
    routes._record_export("org_a", _envelope())   # must not raise


def test_export_telemetry_event_type_is_registered():
    """record_event raises ValueError for an unregistered type, so the event must
    be registered before the route can ever emit it."""
    from app.telemetry import REGISTERED_EVENT_TYPES

    assert "export.evidence_generated" in REGISTERED_EVENT_TYPES


def test_export_audit_event_type_is_registered():
    from app.middleware.audit import AUDIT_EVENT_REGISTRY, EVIDENCE_EXPORT_GENERATED

    assert EVIDENCE_EXPORT_GENERATED in AUDIT_EVENT_REGISTRY
