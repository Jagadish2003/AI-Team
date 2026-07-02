"""
R17-A4 / T5 — .NET operational signals plug into the cross-system corroboration
flow (AC6).

The heart of this story: a .NET runtime signal can SUPPORT and STRENGTHEN a finding
that already exists in another connected system. When ServiceNow shows an incident
spike for a service AND the .NET application shows a matching rise in errors/latency,
the two corroborate — moving AgentIQ from "a ticket system shows a problem" to "the
ticket system AND the actual application runtime both show the same problem".

These exercise the pure corroboration-engine functions plus the real .NET signal
producer (offline), so no DB is required. They deliberately assert that .NET reuses
the SAME cross-system corroboration approach as every other source (COR-10 is an
elevating observed-evidence corroborator, subject to the same single-source ceiling)
— no separate .NET confidence model.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.corroboration_engine import (
    apply_corroboration_confidence,
    build_corroboration_run_data,
    check_cor10_dotnet_app_operational,
    evaluate_corroboration,
)
from discovery.ingest import change_runner
from discovery.ingest.dotnet_app import DotNetAppIngestor
from discovery.ingest.dotnet_app_signals import build_dotnet_app_corroboration_payload

DETECTOR = "ORDERS_PROCESS_FRICTION"
RUN_TS = datetime(2026, 6, 20, tzinfo=timezone.utc)        # within 30d of fixture
FRESH_TS = "2026-06-10T08:10:00+00:00"
STALE_TS = "2026-01-01T00:00:00+00:00"                      # > 30 days before RUN_TS


def _dotnet_block(fired=True, ts=FRESH_TS, services=("orders",)):
    return {
        "dotnet_app": {
            "operational_friction": {
                "fired": fired,
                "timestamp": ts,
                "services": list(services),
                "reasons": ["elevated error rate", "latency degradation"],
            }
        }
    }


def _run_data(connected, **systems):
    data = {"connected_systems": list(connected)}
    data.update(systems)
    return data


# ─────────────────────────────────────────────────────────────────────────────
# COR-10 check — fires on fresh friction, respects the window
# ─────────────────────────────────────────────────────────────────────────────
def test_cor10_fires_for_fresh_operational_friction():
    assert check_cor10_dotnet_app_operational(_run_data(["salesforce", "dotnet_app"], **_dotnet_block()), RUN_TS) is True


def test_cor10_does_not_fire_when_not_fired():
    assert check_cor10_dotnet_app_operational(_run_data(["salesforce", "dotnet_app"], **_dotnet_block(fired=False)), RUN_TS) is False


def test_cor10_respects_the_30_day_window():
    assert check_cor10_dotnet_app_operational(_run_data(["salesforce", "dotnet_app"], **_dotnet_block(ts=STALE_TS)), RUN_TS) is False


def test_cor10_absent_block_does_not_fire():
    assert check_cor10_dotnet_app_operational(_run_data(["salesforce"]), RUN_TS) is False


# ─────────────────────────────────────────────────────────────────────────────
# AC6 — .NET signal is first-class observed evidence and ELEVATES with another system
# ─────────────────────────────────────────────────────────────────────────────
def test_dotnet_app_elevates_because_it_is_observed_evidence():
    rd = _run_data(["salesforce", "dotnet_app"], **_dotnet_block())
    result = evaluate_corroboration(DETECTOR, "service_cloud", rd, RUN_TS, "org-1")
    assert "COR-10" in result.rule_ids
    assert ".NET application (operational signal)" in result.corroboration_sources
    assert result.elevated_confidence == "HIGH"
    assert result.confidence_elevated is True


def test_dotnet_app_corroborates_servicenow_incident_spike():
    # The story's worked example: a ServiceNow incident spike AND a matching .NET
    # error/latency rise for the same service — both COR-01 and COR-10 fire.
    rd = _run_data(
        ["servicenow", "dotnet_app"],
        servicenow={"incidents": [{"state": "Open", "sys_created_on": FRESH_TS,
                                    "detector_ids": [DETECTOR]}]},
        **_dotnet_block(),
    )
    result = evaluate_corroboration(DETECTOR, "service_cloud", rd, RUN_TS, "org-1")
    assert {"COR-01", "COR-10"} <= set(result.rule_ids)
    assert result.elevated_confidence == "HIGH"


def test_elevation_applies_over_a_medium_scorer_baseline():
    rd = _run_data(["salesforce", "dotnet_app"], **_dotnet_block())
    result = evaluate_corroboration(DETECTOR, "service_cloud", rd, RUN_TS, "org-1")
    assert apply_corroboration_confidence("MEDIUM", result) == "HIGH"
    # Never downgrades an already-HIGH scorer verdict.
    assert apply_corroboration_confidence("HIGH", result) == "HIGH"


def test_dotnet_app_alone_is_single_source_and_does_not_self_corroborate():
    # No separate .NET confidence model: a lone .NET source has no finding to
    # corroborate and stays MEDIUM (COR-08), exactly like a lone Java source.
    rd = _run_data(["dotnet_app"], **_dotnet_block())
    result = evaluate_corroboration(DETECTOR, "service_cloud", rd, RUN_TS, "org-1")
    assert result.elevated_confidence == "MEDIUM"
    assert result.confidence_elevated is False


# ─────────────────────────────────────────────────────────────────────────────
# run_data builder threads the dotnet_app block through only when connected
# ─────────────────────────────────────────────────────────────────────────────
def test_build_run_data_includes_dotnet_app_block_when_connected():
    rd = build_corroboration_run_data(
        systems={"salesforce", "dotnet_app"},
        sn_by_detector={}, jira_by_detector={},
        run_timestamp_iso=RUN_TS.isoformat(),
        source_payloads=[_dotnet_block()],
    )
    assert rd["dotnet_app"]["operational_friction"]["fired"] is True


def test_build_run_data_omits_dotnet_app_when_not_connected():
    rd = build_corroboration_run_data(
        systems={"salesforce", "servicenow"},
        sn_by_detector={}, jira_by_detector={},
        run_timestamp_iso=RUN_TS.isoformat(),
        source_payloads=[_dotnet_block()],
    )
    assert "dotnet_app" not in rd


# ─────────────────────────────────────────────────────────────────────────────
# AC6 — end-to-end: ingest the real .NET operational surface → corroborate → elevate
# ─────────────────────────────────────────────────────────────────────────────
def test_finding_grounded_in_real_dotnet_operational_data(monkeypatch):
    monkeypatch.setenv("INGEST_MODE", "offline")
    monkeypatch.setattr("app.telemetry.record_event", lambda *a, **k: None)

    collected: list = []
    store = {}
    change_runner.ingest_with_checkpoint(
        DotNetAppIngestor(), "org-1",
        process_batch=lambda b: collected.extend(b.records),
        read_checkpoint=lambda o, c: store.get((o, c)),
        save_checkpoint=lambda cp: store.__setitem__((cp.org_id, cp.connector_id), cp),
    )
    assert collected, "expected operational records from the .NET application"

    payload = build_dotnet_app_corroboration_payload(collected)
    assert payload["dotnet_app"]["operational_friction"]["fired"] is True

    rd = build_corroboration_run_data(
        systems={"servicenow", "dotnet_app"},
        sn_by_detector={DETECTOR: [{"state": "Open", "sys_created_on": FRESH_TS}]},
        jira_by_detector={},
        run_timestamp_iso=RUN_TS.isoformat(),
        source_payloads=[payload],
    )
    result = evaluate_corroboration(DETECTOR, "service_cloud", rd, RUN_TS, "org-1")
    # The ticket system AND the running application both show the problem → HIGH.
    assert {"COR-01", "COR-10"} <= set(result.rule_ids)
    assert result.elevated_confidence == "HIGH"
    assert apply_corroboration_confidence("MEDIUM", result) == "HIGH"
