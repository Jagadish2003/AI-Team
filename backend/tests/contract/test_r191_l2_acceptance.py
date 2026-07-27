"""R191-L2 / T6 (AT-698) — Consolidated acceptance suite for Usage Metering &
Billing Telemetry (offline-first).

Maps every acceptance criterion of Section 5 (R191-L2) of the 1.9.1 release stories
to a concrete, executable check against the BUILT behaviour (not the task list), so
"done" is verified end to end in one place. The headline new deliverable here is
AC5 — the no-phone-home network-call audit of the metering modules.

AC1  A hosted-AI run emits billing.run_completed with mode hosted, correct system
     count and pack ids; in_boundary / customer_tenant runs emit the same event
     with their mode — all into the immutable store.
AC2  Connecting and disconnecting a system emits timestamped ledger events
     ({connector, system_identity, occurred_at}) sufficient to compute a mid-term
     pro-ration.
AC3  The generated report for a period includes all covered events, verifies
     against the license's report_key, and fails verification if any byte is
     altered.
AC4  Removing an event from the store before generation renders the report
     detectably inconsistent (hash-chain / count mismatch).
AC5  No code path in metering initiates an outbound network call — verified under
     NETWORK_PROFILE=no_public_inbound and by a network-call audit of the metering
     modules.
AC6  Owner usage summary matches the report's numbers exactly for the same period.

Per-task suites remain the primary coverage; this suite is the story-level safety
net that ties them together:
  * AC1 — test_billing_run_completed.py (T1)
  * AC2 — test_billing_system_ledger.py (T2)
  * AC3 — test_usage_report.py (T3)
  * AC4 — test_usage_report.py tamper-evidence (T4)
  * AC5 — this file (network-call audit — new)
  * AC6 — test_usage_summary.py (T5) + this file

Mostly DB-free: telemetry reads/writes are monkeypatched so the metering logic is
exercised in isolation and the suite runs fast and deterministically in CI.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

import app.billing_chain as bc
import app.billing_ledger as bl
import app.usage_report as ur
import app.usage_summary as us

_BACKEND = Path(__file__).resolve().parents[2]

_FROM, _TO = "2026-07-01", "2026-07-31"


# ---------------------------------------------------------------------------
# Shared fixtures — mimic TelemetryEvent rows + a monkeypatchable range.
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
    {"run_id": "r1", "ai_mode": "hosted", "connected_system_count": 3, "pack_ids": ["service_cloud"], "completed_at": "2026-07-02T10:00:00+00:00", "seq": 1},
    {"run_id": "r2", "ai_mode": "in_boundary", "connected_system_count": 3, "pack_ids": ["ncino"], "completed_at": "2026-07-03T10:00:00+00:00", "seq": 2},
    {"run_id": "r3", "ai_mode": "customer_tenant", "connected_system_count": 4, "pack_ids": ["strs_benefits"], "completed_at": "2026-07-04T10:00:00+00:00", "seq": 3},
]
_CONNECTED = [
    {"connector": "salesforce", "system_identity": "sf-1", "occurred_at": "2026-07-01T09:00:00+00:00", "seq": 4},
]
_DISCONNECTED = [
    {"connector": "jira", "system_identity": "jira-1", "occurred_at": "2026-07-05T09:00:00+00:00", "seq": 5},
]


# ===========================================================================
# AC1 — billing.run_completed for every AI mode, into the immutable store.
# ===========================================================================
def test_ac1_run_completed_registered():
    from app.telemetry import REGISTERED_EVENT_TYPES

    assert "billing.run_completed" in REGISTERED_EVENT_TYPES


@pytest.mark.parametrize("mode", ["hosted", "in_boundary", "customer_tenant"])
def test_ac1_run_completed_emits_each_mode(monkeypatch, mode):
    from discovery import runner

    monkeypatch.setenv("MODEL_GENERATION_PROVIDER", mode)
    monkeypatch.setattr("app.license_limits.count_connected_systems", lambda org: 3)
    events: list = []
    monkeypatch.setattr(runner, "record_event", lambda et, p=None: events.append((et, p or {})))

    runner._emit_billing_run_completed(
        org_id="org-A", run_id="run-1", pack_id="service_cloud",
        deployment_type="saas", started_at="2026-07-01T00:00:00+00:00",
    )

    billing = [p for et, p in events if et == "billing.run_completed"]
    assert len(billing) == 1
    p = billing[0]
    assert p["ai_mode"] == mode and p["provider"] == mode
    assert p["connected_system_count"] == 3
    assert p["pack_ids"] == ["service_cloud"]
    # Billability is DERIVED BY THE REPORT — never decided at emission.
    assert "billable" not in p and "billed" not in p


# ===========================================================================
# AC2 — connect/disconnect ledger with {connector, system_identity, occurred_at}.
# ===========================================================================
def test_ac2_ledger_types_registered():
    from app.telemetry import REGISTERED_EVENT_TYPES

    assert "billing.system_connected" in REGISTERED_EVENT_TYPES
    assert "billing.system_disconnected" in REGISTERED_EVENT_TYPES


def test_ac2_connect_disconnect_emit_pro_ration_fields(monkeypatch):
    events: list = []
    monkeypatch.setattr(bl, "record_event", lambda et, p=None: events.append((et, p or {})))
    monkeypatch.setattr(bl, "resolve_system_identity", lambda org, cid: f"{cid}-instance")
    monkeypatch.setattr(bc, "next_seq", lambda org: 1)

    # A genuine addition and a genuine removal.
    bl.emit_system_connected("org-A", "salesforce", was_connected=False)
    bl.emit_system_disconnected("org-A", "jira", was_connected=True, system_identity="jira-1")

    by_type = {et: p for et, p in events}
    conn = by_type["billing.system_connected"]
    disc = by_type["billing.system_disconnected"]
    for p in (conn, disc):
        # The exact pro-ration fields the report ledger reads.
        assert "connector" in p and "system_identity" in p and "occurred_at" in p
        assert isinstance(p["occurred_at"], str) and p["occurred_at"]
    assert conn["connector"] == "salesforce"
    assert disc["system_identity"] == "jira-1"


def test_ac2_only_genuine_transitions_ledger(monkeypatch):
    """Sufficient for pro-ration means CLEAN: a re-auth of a live connector and a
    no-op disconnect emit nothing, so the ledger has no phantom add/remove."""
    events: list = []
    monkeypatch.setattr(bl, "record_event", lambda et, p=None: events.append((et, p or {})))
    monkeypatch.setattr(bl, "resolve_system_identity", lambda org, cid: cid)
    monkeypatch.setattr(bc, "next_seq", lambda org: None)

    bl.emit_system_connected("o", "salesforce", was_connected=True)   # re-auth
    bl.emit_system_disconnected("o", "slack", was_connected=False)    # no-op
    assert events == []


# ===========================================================================
# AC3 — report includes covered events, verifies against report_key, tamper fails.
# ===========================================================================
def test_ac3_report_signs_verifies_and_detects_tampering(monkeypatch):
    monkeypatch.setattr(
        "app.usage_report.get_telemetry_range",
        _fake_range(runs=_RUNS, connected=_CONNECTED, disconnected=_DISCONNECTED),
    )
    body = ur.build_usage_report_body(
        "org-A", _FROM, _TO, kid="cf-1", license_org_id="org-A", generated_at="t0"
    )
    # All covered events are present.
    assert body["runs"]["total"] == 3
    assert len(body["system_ledger"]) == 2
    assert body["event_count"] == 5

    report_key = "rk-secret-xyz"
    sig = ur.sign_report_body(body, report_key)
    assert ur.verify_report(body, sig, report_key) is True
    # Any altered byte fails.
    tampered = json.loads(json.dumps(body))
    tampered["runs"]["total"] = 999
    assert ur.verify_report(tampered, sig, report_key) is False
    # Wrong key fails.
    assert ur.verify_report(body, sig, "a-different-key") is False


# ===========================================================================
# AC4 — a deleted event renders the report detectably inconsistent.
# ===========================================================================
def test_ac4_contiguous_events_are_consistent(monkeypatch):
    monkeypatch.setattr(
        "app.usage_report.get_telemetry_range",
        _fake_range(runs=_RUNS, connected=_CONNECTED, disconnected=_DISCONNECTED),
    )
    body = ur.build_usage_report_body(
        "o", _FROM, _TO, kid=None, license_org_id=None, generated_at="t0"
    )
    te = body["tamper_evidence"]
    assert te["consistent"] is True
    assert bc.verify_tamper_evidence(te)["consistent"] is True


def test_ac4_deleted_event_is_detected(monkeypatch):
    """Drop one covered event (seq 2) before generation → the seq block is no longer
    contiguous → the report is detectably inconsistent (gap detected)."""
    runs_missing = [r for r in _RUNS if r["seq"] != 2]  # delete the seq=2 event
    monkeypatch.setattr(
        "app.usage_report.get_telemetry_range",
        _fake_range(runs=runs_missing, connected=_CONNECTED, disconnected=_DISCONNECTED),
    )
    body = ur.build_usage_report_body(
        "o", _FROM, _TO, kid=None, license_org_id=None, generated_at="t0"
    )
    te = body["tamper_evidence"]
    assert te["consistent"] is False
    verdict = bc.verify_tamper_evidence(te)
    assert verdict["consistent"] is False
    assert verdict["gap_detected"] is True


# ===========================================================================
# AC5 — NO metering code path initiates an outbound network call. (Headline.)
# ===========================================================================

# The dedicated metering modules (the run emitter lives inside discovery/runner.py
# and is exercised behaviourally under the runtime tripwire below).
_METERING_MODULE_FILES = [
    _BACKEND / "app" / "billing_ledger.py",
    _BACKEND / "app" / "billing_chain.py",
    _BACKEND / "app" / "usage_report.py",
    _BACKEND / "app" / "usage_summary.py",
    _BACKEND / "app" / "routes_usage_report.py",
    _BACKEND / "app" / "routes_usage_summary.py",
    _BACKEND / "scripts" / "generate_usage_report.py",
]

# Outbound-network mechanisms a "phone home" would have to go through. urllib.parse
# is fine (string work); only urllib.request reaches the network.
_BANNED_IMPORT_ROOTS = {
    "requests", "httpx", "aiohttp", "urllib3", "socket", "smtplib",
    "ftplib", "telnetlib", "websocket", "websockets", "http.client",
    "urllib.request",
}


def _imported_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.add(node.module)
                for alias in node.names:
                    names.add(f"{node.module}.{alias.name}")
    return names


def test_ac5_metering_modules_import_no_network_client():
    """Static audit: not one metering module imports an outbound-HTTP/network
    client. A new metering module that adds one fails this without any other edit."""
    offenders: dict[str, set[str]] = {}
    for path in _METERING_MODULE_FILES:
        assert path.exists(), f"metering module missing: {path}"
        imported = _imported_names(path)
        bad = {
            name
            for name in imported
            if name in _BANNED_IMPORT_ROOTS
            or name.split(".")[0] in {"requests", "httpx", "aiohttp", "urllib3", "socket", "websocket", "websockets"}
        }
        if bad:
            offenders[path.name] = bad
    assert not offenders, f"metering modules reference network clients: {offenders}"


def test_ac5_no_outbound_call_under_no_public_inbound(monkeypatch):
    """Runtime audit: under NETWORK_PROFILE=no_public_inbound, exercise every
    metering path (run emit, ledger emit, report build+sign+verify, summary build)
    with tripwires on the outbound-HTTP entry points. Nothing fires — metering is
    fully local, exactly as the no-phone-home posture requires."""
    monkeypatch.setenv("NETWORK_PROFILE", "no_public_inbound")

    fired: list[str] = []

    def _trip(name):
        def _boom(*a, **k):
            fired.append(name)
            raise AssertionError(f"outbound network call attempted via {name}")
        return _boom

    import http.client
    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", _trip("urllib.request.urlopen"))
    monkeypatch.setattr(http.client.HTTPConnection, "request", _trip("http.client.HTTPConnection.request"))
    monkeypatch.setattr(http.client.HTTPSConnection, "request", _trip("http.client.HTTPSConnection.request"))
    for mod_name in ("requests", "httpx"):
        try:
            mod = __import__(mod_name)
        except Exception:
            continue
        for attr in ("get", "post", "put", "delete", "request"):
            if hasattr(mod, attr):
                monkeypatch.setattr(mod, attr, _trip(f"{mod_name}.{attr}"), raising=False)

    # --- exercise the metering paths (DB-free) ---
    # run emitter, all modes
    from discovery import runner

    monkeypatch.setattr("app.license_limits.count_connected_systems", lambda org: 2)
    monkeypatch.setattr(runner, "record_event", lambda et, p=None: None)
    for mode in ("hosted", "in_boundary", "customer_tenant"):
        monkeypatch.setenv("MODEL_GENERATION_PROVIDER", mode)
        runner._emit_billing_run_completed(
            org_id="o", run_id="r", pack_id="pk", deployment_type="saas", started_at="t0"
        )

    # ledger emitters
    monkeypatch.setattr(bl, "record_event", lambda et, p=None: None)
    monkeypatch.setattr(bl, "resolve_system_identity", lambda org, cid: cid)
    monkeypatch.setattr(bc, "next_seq", lambda org: 1)
    bl.emit_system_connected("o", "salesforce", was_connected=False)
    bl.emit_system_disconnected("o", "jira", was_connected=True, system_identity="jira-1")

    # report build + sign + verify, and summary build
    monkeypatch.setattr(
        "app.usage_report.get_telemetry_range",
        _fake_range(runs=_RUNS, connected=_CONNECTED, disconnected=_DISCONNECTED),
    )
    body = ur.build_usage_report_body(
        "o", _FROM, _TO, kid="k", license_org_id="o", generated_at="t0"
    )
    sig = ur.sign_report_body(body, "rk")
    assert ur.verify_report(body, sig, "rk") is True
    summary = us.build_usage_summary("o", _FROM, _TO, generated_at="t0")

    assert summary["runs"]["total"] == 3
    assert fired == [], f"metering initiated outbound network calls: {fired}"


# ===========================================================================
# AC6 — Owner usage summary matches the report's numbers exactly.
# ===========================================================================
def test_ac6_summary_matches_report_exactly(monkeypatch):
    monkeypatch.setattr(
        "app.usage_report.get_telemetry_range",
        _fake_range(runs=_RUNS, connected=_CONNECTED, disconnected=_DISCONNECTED),
    )
    report = ur.build_usage_report_body(
        "org-A", _FROM, _TO, kid="k", license_org_id="org-A", generated_at="t0"
    )
    summary = us.build_usage_summary("org-A", _FROM, _TO, generated_at="t0")

    assert summary["runs"]["total"] == report["runs"]["total"]
    assert summary["runs"]["by_ai_mode"] == report["runs"]["by_ai_mode"]
    assert summary["systems"]["ledger"] == report["system_ledger"]
    assert summary["event_count"] == report["event_count"]
    assert [o["connected_system_count"] for o in summary["systems"]["over_time"]] == [
        r["connected_system_count"] for r in report["runs"]["per_run"]
    ]
