"""R-1.9.1-L2 / T3 (AT-695) — signed usage report generator (AC3).

AC3: the generated report for a period includes all covered events, verifies
against the license's ``report_key``, and fails verification if any byte is
altered.

Most tests are DB-free: ``app.usage_report.get_telemetry_range`` and
``get_current_license_status`` are monkeypatched so the aggregation + signing
logic is exercised without a live telemetry store or a real license. The route
tests use the ``client`` fixture (contract Postgres) and run in CI.
"""
from __future__ import annotations

import json

import pytest

from app import usage_report as ur


# ---------------------------------------------------------------------------
# Helpers — fake telemetry events + a fake license
# ---------------------------------------------------------------------------
class _Ev:
    """Mimics a TelemetryEvent: a JSON-string ``payload``."""

    def __init__(self, payload: dict):
        self.payload = json.dumps(payload)


def _fake_range(runs=None, connected=None, disconnected=None):
    def _range(org_id, event_type, from_dt, to_dt, limit=10000):
        if event_type == ur.BILLING_RUN_COMPLETED:
            return [_Ev(p) for p in (runs or [])]
        if event_type == ur.BILLING_SYSTEM_CONNECTED:
            return [_Ev(p) for p in (connected or [])]
        if event_type == ur.BILLING_SYSTEM_DISCONNECTED:
            return [_Ev(p) for p in (disconnected or [])]
        return []

    return _range


def _install_license(monkeypatch, *, report_key="rk-secret-123", kid="cf-2026-1", org_id="org-A"):
    payload = {}
    if report_key is not None:
        payload["report_key"] = report_key
    if kid is not None:
        payload["kid"] = kid
    if org_id is not None:
        payload["org_id"] = org_id
    monkeypatch.setattr(
        "app.usage_report.get_current_license_status",
        lambda **k: {"status": "valid", "payload": payload},
    )


_RUNS = [
    {"run_id": "r1", "ai_mode": "hosted", "connected_system_count": 3, "pack_ids": ["service_cloud"], "completed_at": "2026-07-02T10:00:00+00:00"},
    {"run_id": "r2", "ai_mode": "hosted", "connected_system_count": 3, "pack_ids": ["service_cloud"], "completed_at": "2026-07-03T10:00:00+00:00"},
    {"run_id": "r3", "ai_mode": "in_boundary", "connected_system_count": 2, "pack_ids": ["ncino"], "completed_at": "2026-07-04T10:00:00+00:00"},
    {"run_id": "r4", "ai_mode": "customer_tenant", "connected_system_count": 5, "pack_ids": ["strs_benefits"], "completed_at": "2026-07-05T10:00:00+00:00"},
]
_CONNECTED = [
    {"connector": "salesforce", "system_identity": "sf-1", "occurred_at": "2026-07-01T09:00:00+00:00"},
]
_DISCONNECTED = [
    {"connector": "jira", "system_identity": "jira-1", "occurred_at": "2026-07-06T09:00:00+00:00"},
]


# ---------------------------------------------------------------------------
# Signing / verification core (AC3) — pure, no DB
# ---------------------------------------------------------------------------
def test_sign_and_verify_roundtrip():
    body = {"a": 1, "b": ["x", "y"], "c": {"z": True}}
    sig = ur.sign_report_body(body, "rk-secret")
    assert isinstance(sig, str) and sig
    assert ur.verify_report(body, sig, "rk-secret") is True


def test_any_altered_byte_fails_verification():
    body = {"a": 1, "b": ["x", "y"]}
    sig = ur.sign_report_body(body, "rk-secret")
    tampered = {"a": 2, "b": ["x", "y"]}  # one value changed
    assert ur.verify_report(tampered, sig, "rk-secret") is False
    tampered2 = {"a": 1, "b": ["x", "y", "z"]}  # a list element added
    assert ur.verify_report(tampered2, sig, "rk-secret") is False


def test_wrong_report_key_fails_verification():
    body = {"a": 1}
    sig = ur.sign_report_body(body, "rk-secret")
    assert ur.verify_report(body, sig, "a-different-key") is False


def test_signature_is_deterministic():
    body = {"b": 2, "a": 1, "nested": {"y": 2, "x": 1}}
    assert ur.sign_report_body(body, "rk") == ur.sign_report_body(body, "rk")
    # Key order in the source dict must not change the signature (canonical form).
    reordered = {"a": 1, "nested": {"x": 1, "y": 2}, "b": 2}
    assert ur.sign_report_body(reordered, "rk") == ur.sign_report_body(body, "rk")


# ---------------------------------------------------------------------------
# Report assembly — aggregation of billing events (no DB)
# ---------------------------------------------------------------------------
def test_build_aggregates_runs_by_ai_mode_and_per_run(monkeypatch):
    monkeypatch.setattr("app.usage_report.get_telemetry_range", _fake_range(runs=_RUNS))
    body = ur.build_usage_report_body(
        "org-A", "2026-07-01", "2026-07-31", kid="cf-2026-1", license_org_id="org-A",
        generated_at="2026-08-01T00:00:00+00:00",
    )
    assert body["runs"]["total"] == 4
    assert body["runs"]["by_ai_mode"] == {"hosted": 2, "in_boundary": 1, "customer_tenant": 1}
    # per-run system counts are carried for each run.
    per_run = {r["run_id"]: r for r in body["runs"]["per_run"]}
    assert per_run["r1"]["connected_system_count"] == 3
    assert per_run["r4"]["ai_mode"] == "customer_tenant"
    assert per_run["r3"]["pack_ids"] == ["ncino"]


def test_build_includes_connect_disconnect_ledger(monkeypatch):
    monkeypatch.setattr(
        "app.usage_report.get_telemetry_range",
        _fake_range(connected=_CONNECTED, disconnected=_DISCONNECTED),
    )
    body = ur.build_usage_report_body(
        "org-A", "2026-07-01", "2026-07-31", kid="cf-2026-1", license_org_id="org-A",
        generated_at="2026-08-01T00:00:00+00:00",
    )
    kinds = [(e["event"], e["connector"]) for e in body["system_ledger"]]
    assert ("connected", "salesforce") in kinds
    assert ("disconnected", "jira") in kinds


def test_report_includes_all_covered_events(monkeypatch):
    """AC3: the report accounts for every covered event (event_count == total)."""
    monkeypatch.setattr(
        "app.usage_report.get_telemetry_range",
        _fake_range(runs=_RUNS, connected=_CONNECTED, disconnected=_DISCONNECTED),
    )
    body = ur.build_usage_report_body(
        "org-A", "2026-07-01", "2026-07-31", kid="cf-2026-1", license_org_id="org-A",
        generated_at="2026-08-01T00:00:00+00:00",
    )
    assert body["event_count"] == len(_RUNS) + len(_CONNECTED) + len(_DISCONNECTED)
    assert body["kid"] == "cf-2026-1"
    assert body["org_id"] == "org-A"
    assert body["period"] == {"from": "2026-07-01", "to": "2026-07-31"}
    assert body["generated_at"] == "2026-08-01T00:00:00+00:00"


def test_period_from_after_to_raises(monkeypatch):
    monkeypatch.setattr("app.usage_report.get_telemetry_range", _fake_range())
    with pytest.raises(ur.UsageReportError):
        ur.build_usage_report_body(
            "org-A", "2026-07-31", "2026-07-01", kid=None, license_org_id=None,
            generated_at="t",
        )


def test_malformed_period_raises(monkeypatch):
    monkeypatch.setattr("app.usage_report.get_telemetry_range", _fake_range())
    with pytest.raises(ur.UsageReportError):
        ur.build_usage_report_body(
            "org-A", "not-a-date", "2026-07-01", kid=None, license_org_id=None, generated_at="t"
        )


# ---------------------------------------------------------------------------
# End-to-end signing with the license report_key (no DB)
# ---------------------------------------------------------------------------
def test_generate_signed_report_uses_license_report_key(monkeypatch):
    _install_license(monkeypatch, report_key="rk-abc", kid="cf-2027-2", org_id="org-XYZ")
    monkeypatch.setattr(
        "app.usage_report.get_telemetry_range",
        _fake_range(runs=_RUNS, connected=_CONNECTED, disconnected=_DISCONNECTED),
    )

    env = ur.generate_signed_report("org-XYZ", "2026-07-01", "2026-07-31")

    assert set(env) == {"report", "signature", "algorithm"}
    assert env["algorithm"] == "HMAC-SHA256"
    body = env["report"]
    assert body["kid"] == "cf-2027-2"
    assert body["license_org_id"] == "org-XYZ"
    assert body["event_count"] == 6
    # AC3: the report verifies against the license's report_key...
    assert ur.verify_report(body, env["signature"], "rk-abc") is True
    # ...and any altered byte fails verification.
    body_altered = dict(body)
    body_altered["event_count"] = 999
    assert ur.verify_report(body_altered, env["signature"], "rk-abc") is False


def test_generate_signed_report_no_report_key_raises(monkeypatch):
    _install_license(monkeypatch, report_key=None)  # license carries no report_key
    monkeypatch.setattr("app.usage_report.get_telemetry_range", _fake_range())
    with pytest.raises(ur.UsageReportError):
        ur.generate_signed_report("org-A", "2026-07-01", "2026-07-31")


def test_generate_is_deterministic_for_fixed_generated_at(monkeypatch):
    _install_license(monkeypatch, report_key="rk-abc")
    monkeypatch.setattr("app.usage_report.get_telemetry_range", _fake_range(runs=_RUNS))
    a = ur.generate_signed_report("org-A", "2026-07-01", "2026-07-31", generated_at="2026-08-01T00:00:00+00:00")
    b = ur.generate_signed_report("org-A", "2026-07-01", "2026-07-31", generated_at="2026-08-01T00:00:00+00:00")
    assert a["signature"] == b["signature"]


# ---------------------------------------------------------------------------
# AC5 posture — the generator makes no outbound network call.
# ---------------------------------------------------------------------------
def test_module_has_no_outbound_network_imports():
    import inspect

    src = inspect.getsource(ur)
    for lib in ("requests", "httpx", "aiohttp", "urllib.request", "http.client", "socket"):
        assert f"import {lib}" not in src, f"usage_report must not import {lib}"
        assert f"from {lib}" not in src, f"usage_report must not import from {lib}"


# ---------------------------------------------------------------------------
# Route — Owner-only signed report (contract Postgres → CI)
# ---------------------------------------------------------------------------
AUTH = {"Authorization": "Bearer dev-token-change-me"}


def _set_role(role: str) -> dict:
    import uuid
    from datetime import datetime, timezone

    from app import db
    from app.rbac import _ensure_members_table

    _ensure_members_table()
    org_id = f"usage_{role}_{uuid.uuid4().hex[:8]}"
    con = db.connect()
    try:
        con.execute(
            "INSERT INTO workspace_members (org_id, user_id, role, created_at) "
            "VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (org_id, user_id) DO UPDATE SET role=EXCLUDED.role, created_at=EXCLUDED.created_at",
            (org_id, "dev-token-change-me", role, datetime.now(timezone.utc).isoformat()),
        )
        con.commit()
    finally:
        con.close()
    return {**AUTH, "X-Org-Id": org_id}


def test_route_owner_returns_signed_report(client, monkeypatch):
    _install_license(monkeypatch, report_key="rk-route", kid="cf-2026-1")
    monkeypatch.setattr("app.usage_report.get_telemetry_range", _fake_range(runs=_RUNS))
    headers = _set_role("owner")

    resp = client.get("/api/usage/report?from=2026-07-01&to=2026-07-31", headers=headers)

    assert resp.status_code == 200, resp.text
    env = resp.json()
    assert env["algorithm"] == "HMAC-SHA256"
    assert env["report"]["runs"]["total"] == 4
    assert ur.verify_report(env["report"], env["signature"], "rk-route") is True


def test_route_no_report_key_is_400(client, monkeypatch):
    _install_license(monkeypatch, report_key=None)
    monkeypatch.setattr("app.usage_report.get_telemetry_range", _fake_range())
    headers = _set_role("owner")
    resp = client.get("/api/usage/report?from=2026-07-01&to=2026-07-31", headers=headers)
    assert resp.status_code == 400


@pytest.mark.parametrize("role", ["analyst", "viewer"])
def test_route_non_owner_forbidden(client, role):
    headers = _set_role(role)
    resp = client.get("/api/usage/report?from=2026-07-01&to=2026-07-31", headers=headers)
    assert resp.status_code == 403
