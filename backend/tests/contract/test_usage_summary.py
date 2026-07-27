"""R-1.9.1-L2 / T5 (AT-697) — Owner-facing usage summary (AC6).

The pre-invoice VISIBILITY view: an Owner-facing summary (counts per AI mode +
systems over time) whose numbers MATCH the signed usage report's numbers exactly
for the same period. The summary is a projection of the same aggregation
(``usage_report.build_usage_report_body``), so AC6 holds by construction.

Most tests are DB-free: ``usage_report.get_telemetry_range`` is monkeypatched so
the aggregation runs against fixed events. The route tests use the contract
Postgres (workspace_members for role resolution) — CI.
"""
from __future__ import annotations

import json

import pytest

import app.usage_report as ur
import app.usage_summary as us


# ---------------------------------------------------------------------------
# Fixtures — mimic TelemetryEvent rows and a monkeypatchable telemetry range.
# ---------------------------------------------------------------------------
class _Ev:
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


_RUNS = [
    {"run_id": "r1", "ai_mode": "hosted", "connected_system_count": 3, "pack_ids": ["service_cloud"], "completed_at": "2026-07-02T10:00:00+00:00"},
    {"run_id": "r2", "ai_mode": "hosted", "connected_system_count": 3, "pack_ids": ["service_cloud"], "completed_at": "2026-07-03T10:00:00+00:00"},
    {"run_id": "r3", "ai_mode": "in_boundary", "connected_system_count": 4, "pack_ids": ["ncino"], "completed_at": "2026-07-04T10:00:00+00:00"},
    {"run_id": "r4", "ai_mode": "customer_tenant", "connected_system_count": 5, "pack_ids": ["strs_benefits"], "completed_at": "2026-07-05T10:00:00+00:00"},
]
_CONNECTED = [
    {"connector": "salesforce", "system_identity": "sf-1", "occurred_at": "2026-07-01T09:00:00+00:00"},
    {"connector": "jira", "system_identity": "jira-1", "occurred_at": "2026-07-02T09:00:00+00:00"},
]
_DISCONNECTED = [
    {"connector": "slack", "system_identity": "slack-1", "occurred_at": "2026-07-06T09:00:00+00:00"},
]

_FROM, _TO = "2026-07-01", "2026-07-31"


# ---------------------------------------------------------------------------
# Shape + content (DB-free)
# ---------------------------------------------------------------------------
def test_summary_full_shape(monkeypatch):
    monkeypatch.setattr(
        "app.usage_report.get_telemetry_range",
        _fake_range(runs=_RUNS, connected=_CONNECTED, disconnected=_DISCONNECTED),
    )
    s = us.build_usage_summary("org-A", _FROM, _TO, generated_at="t0")

    assert s["summary_version"] == us.SUMMARY_VERSION
    assert s["org_id"] == "org-A"
    assert s["period"] == {"from": _FROM, "to": _TO}
    assert s["generated_at"] == "t0"
    assert set(s["runs"]) == {"total", "by_ai_mode", "billable"}
    assert set(s["systems"]) == {"connected", "disconnected", "net_change", "ledger", "over_time"}
    assert "event_count" in s


def test_counts_per_ai_mode(monkeypatch):
    monkeypatch.setattr("app.usage_report.get_telemetry_range", _fake_range(runs=_RUNS))
    s = us.build_usage_summary("o", _FROM, _TO, generated_at="t0")
    assert s["runs"]["total"] == 4
    assert s["runs"]["by_ai_mode"] == {"hosted": 2, "in_boundary": 1, "customer_tenant": 1}
    # hosted is the billable subset.
    assert s["runs"]["billable"] == 2


def test_systems_over_time_and_ledger(monkeypatch):
    monkeypatch.setattr(
        "app.usage_report.get_telemetry_range",
        _fake_range(runs=_RUNS, connected=_CONNECTED, disconnected=_DISCONNECTED),
    )
    s = us.build_usage_summary("o", _FROM, _TO, generated_at="t0")

    assert s["systems"]["connected"] == 2
    assert s["systems"]["disconnected"] == 1
    assert s["systems"]["net_change"] == 1
    # The ledger is the full timestamped connect/disconnect record.
    kinds = {(e["event"], e["connector"]) for e in s["systems"]["ledger"]}
    assert ("connected", "salesforce") in kinds
    assert ("disconnected", "slack") in kinds
    # Systems over time: one entry per run, carrying the connected-system count.
    over = s["systems"]["over_time"]
    assert [o["connected_system_count"] for o in over] == [3, 3, 4, 5]
    assert [o["run_id"] for o in over] == ["r1", "r2", "r3", "r4"]


# ---------------------------------------------------------------------------
# AC6 — the summary numbers MATCH the report's numbers exactly for the period.
# ---------------------------------------------------------------------------
def test_summary_matches_report_numbers_exactly(monkeypatch):
    monkeypatch.setattr(
        "app.usage_report.get_telemetry_range",
        _fake_range(runs=_RUNS, connected=_CONNECTED, disconnected=_DISCONNECTED),
    )
    report = ur.build_usage_report_body(
        "org-A", _FROM, _TO, kid="k", license_org_id="org-A", generated_at="t0"
    )
    summary = us.build_usage_summary("org-A", _FROM, _TO, generated_at="t0")

    # Run counts + per-mode breakdown are identical.
    assert summary["runs"]["total"] == report["runs"]["total"]
    assert summary["runs"]["by_ai_mode"] == report["runs"]["by_ai_mode"]
    # The system ledger is identical (same rows, same order).
    assert summary["systems"]["ledger"] == report["system_ledger"]
    # Event count is identical.
    assert summary["event_count"] == report["event_count"]
    # Per-run system counts match the report's per_run series exactly.
    assert [o["connected_system_count"] for o in summary["systems"]["over_time"]] == [
        r["connected_system_count"] for r in report["runs"]["per_run"]
    ]
    assert [o["run_id"] for o in summary["systems"]["over_time"]] == [
        r["run_id"] for r in report["runs"]["per_run"]
    ]


def test_summary_matches_report_when_empty(monkeypatch):
    """No billing events in the period → both the report and the summary read zero,
    identically (AC6 holds at the boundary too)."""
    monkeypatch.setattr("app.usage_report.get_telemetry_range", _fake_range())
    report = ur.build_usage_report_body(
        "o", _FROM, _TO, kid=None, license_org_id=None, generated_at="t0"
    )
    summary = us.build_usage_summary("o", _FROM, _TO, generated_at="t0")
    assert summary["runs"]["total"] == report["runs"]["total"] == 0
    assert summary["event_count"] == report["event_count"] == 0
    assert summary["systems"]["ledger"] == report["system_ledger"] == []


# ---------------------------------------------------------------------------
# The summary needs NO report_key / no installed license (it is a preview).
# ---------------------------------------------------------------------------
def test_summary_needs_no_report_key(monkeypatch):
    """Unlike the signed report, the summary must build with no license/report_key —
    the Owner previews usage even before a key is provisioned. It must never touch
    the license-resolution path."""
    monkeypatch.setattr("app.usage_report.get_telemetry_range", _fake_range(runs=_RUNS))

    def _boom(*a, **k):
        raise AssertionError("summary must not resolve the license/report_key")

    monkeypatch.setattr("app.usage_report.get_current_license_status", _boom)
    s = us.build_usage_summary("o", _FROM, _TO, generated_at="t0")
    assert s["runs"]["total"] == 4


# ---------------------------------------------------------------------------
# Period validation is reused from the report (one clear error).
# ---------------------------------------------------------------------------
def test_malformed_period_raises(monkeypatch):
    monkeypatch.setattr("app.usage_report.get_telemetry_range", _fake_range())
    with pytest.raises(ur.UsageReportError):
        us.build_usage_summary("o", "not-a-date", _TO, generated_at="t0")


def test_period_from_after_to_raises(monkeypatch):
    monkeypatch.setattr("app.usage_report.get_telemetry_range", _fake_range())
    with pytest.raises(ur.UsageReportError):
        us.build_usage_summary("o", "2026-07-31", "2026-07-01", generated_at="t0")


# ---------------------------------------------------------------------------
# AC5 hygiene — the summary module initiates no outbound network call.
# ---------------------------------------------------------------------------
def test_module_has_no_outbound_network_imports():
    import inspect

    src = inspect.getsource(us)
    for banned in ("requests", "httpx", "urllib.request", "urlopen", "socket"):
        assert banned not in src, f"usage_summary must not reference {banned}"


# ---------------------------------------------------------------------------
# Route — Owner-only (contract Postgres → CI)
# ---------------------------------------------------------------------------
AUTH = {"Authorization": "Bearer dev-token-change-me"}


def _set_role(role: str) -> dict:
    import uuid
    from datetime import datetime, timezone

    from app import db
    from app.rbac import _ensure_members_table

    _ensure_members_table()
    org_id = f"usagesum_{role}_{uuid.uuid4().hex[:8]}"
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


def test_route_owner_returns_summary(client, monkeypatch):
    monkeypatch.setattr("app.usage_report.get_telemetry_range", _fake_range(runs=_RUNS))
    headers = _set_role("owner")

    resp = client.get(f"/api/usage/summary?from={_FROM}&to={_TO}", headers=headers)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["runs"]["total"] == 4
    assert body["runs"]["by_ai_mode"] == {"hosted": 2, "in_boundary": 1, "customer_tenant": 1}
    assert body["runs"]["billable"] == 2


def test_route_bad_period_is_400(client):
    headers = _set_role("owner")
    resp = client.get("/api/usage/summary?from=nope&to=2026-07-31", headers=headers)
    assert resp.status_code == 400


@pytest.mark.parametrize("role", ["analyst", "viewer"])
def test_route_non_owner_forbidden(client, role):
    headers = _set_role(role)
    resp = client.get(f"/api/usage/summary?from={_FROM}&to={_TO}", headers=headers)
    assert resp.status_code == 403
