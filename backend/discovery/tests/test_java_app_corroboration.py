"""
R17-A3 / T5 + T7 — Java-app operational signal corroboration (AC5, AC7).

  * AC5 — a Java-app operational signal can corroborate a finding in another
    system (e.g. an error-rate rise corroborating a ServiceNow incident spike)
    and contribute to confidence. Unlike the Slack ceiling (COR-05), Java-app
    operational friction is first-class OBSERVED evidence (R17-A3 §3), so COR-09
    is an ELEVATING corroborator.
  * AC7 — the definition-of-done: a discovery run produces a finding grounded in
    Java-application operational data. Demonstrated end-to-end: ingest the Java
    operational surface → build the corroboration block → the engine elevates a
    finding's confidence on the strength of the Java signal.

These exercise the pure engine functions plus the real ingestor (offline), so no
DB is required.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.corroboration_engine import (
    apply_corroboration_confidence,
    build_corroboration_run_data,
    check_cor09_java_app_operational,
    evaluate_corroboration,
)
from discovery.ingest import change_runner
from discovery.ingest.base import Checkpoint
from discovery.ingest.java_app import JavaAppIngestor
from discovery.ingest.java_app_signals import build_java_app_corroboration_payload

DETECTOR = "PAYMENTS_PROCESS_FRICTION"
RUN_TS = datetime(2026, 6, 20, tzinfo=timezone.utc)        # within 30d of fixture
FRESH_TS = "2026-06-10T08:10:00+00:00"
STALE_TS = "2026-01-01T00:00:00+00:00"                      # > 30 days before RUN_TS


def _java_block(fired=True, ts=FRESH_TS, services=("payments",)):
    return {
        "java_app": {
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
# COR-09 check
# ─────────────────────────────────────────────────────────────────────────────
def test_cor09_fires_for_fresh_operational_friction():
    rd = _run_data(["salesforce", "java_app"], **_java_block())
    assert check_cor09_java_app_operational(rd, RUN_TS) is True


def test_cor09_does_not_fire_when_not_fired():
    rd = _run_data(["salesforce", "java_app"], **_java_block(fired=False))
    assert check_cor09_java_app_operational(rd, RUN_TS) is False


def test_cor09_respects_the_30_day_window():
    rd = _run_data(["salesforce", "java_app"], **_java_block(ts=STALE_TS))
    assert check_cor09_java_app_operational(rd, RUN_TS) is False


def test_cor09_absent_block_does_not_fire():
    assert check_cor09_java_app_operational(_run_data(["salesforce"]), RUN_TS) is False


# ─────────────────────────────────────────────────────────────────────────────
# AC5 — Java-app signal contributes to confidence (elevates), like a SoR
# ─────────────────────────────────────────────────────────────────────────────
def test_java_app_alone_elevates_because_it_is_observed_evidence():
    # Salesforce finding + Java-app operational friction (2 systems → not single
    # source). Java is first-class observed evidence, so it elevates to HIGH.
    rd = _run_data(["salesforce", "java_app"], **_java_block())
    result = evaluate_corroboration(DETECTOR, "service_cloud", rd, RUN_TS, "org-1")
    assert "COR-09" in result.rule_ids
    assert "Java application (operational signal)" in result.corroboration_sources
    assert result.elevated_confidence == "HIGH"
    assert result.confidence_elevated is True


def test_java_app_corroborates_servicenow_incident_spike():
    # The story's example: a Java error-rate rise corroborating a ServiceNow
    # incident spike for the same service. Both COR-01 and COR-09 fire.
    rd = _run_data(
        ["servicenow", "java_app"],
        servicenow={"incidents": [{"state": "Open", "sys_created_on": FRESH_TS,
                                    "detector_ids": [DETECTOR]}]},
        **_java_block(),
    )
    result = evaluate_corroboration(DETECTOR, "service_cloud", rd, RUN_TS, "org-1")
    assert {"COR-01", "COR-09"} <= set(result.rule_ids)
    assert result.elevated_confidence == "HIGH"


def test_elevation_applies_over_a_medium_scorer_baseline():
    rd = _run_data(["salesforce", "java_app"], **_java_block())
    result = evaluate_corroboration(DETECTOR, "service_cloud", rd, RUN_TS, "org-1")
    # The pipeline never downgrades; a MEDIUM scorer baseline lifts to HIGH.
    assert apply_corroboration_confidence("MEDIUM", result) == "HIGH"
    # And a scorer that already said HIGH is preserved.
    assert apply_corroboration_confidence("HIGH", result) == "HIGH"


def test_java_app_alone_as_single_source_does_not_elevate():
    # Only the Java app connected → cannot self-corroborate (COR-08).
    rd = _run_data(["java_app"], **_java_block())
    result = evaluate_corroboration(DETECTOR, "service_cloud", rd, RUN_TS, "org-1")
    assert result.elevated_confidence == "MEDIUM"
    assert result.confidence_elevated is False


# ─────────────────────────────────────────────────────────────────────────────
# run_data builder threads the java_app block through
# ─────────────────────────────────────────────────────────────────────────────
def test_build_run_data_includes_java_app_block_when_connected():
    payload = _java_block()
    rd = build_corroboration_run_data(
        systems={"salesforce", "java_app"},
        sn_by_detector={},
        jira_by_detector={},
        run_timestamp_iso=RUN_TS.isoformat(),
        source_payloads=[payload],
    )
    assert "java_app" in rd
    assert rd["java_app"]["operational_friction"]["fired"] is True


def test_build_run_data_omits_java_app_when_not_connected():
    rd = build_corroboration_run_data(
        systems={"salesforce", "servicenow"},
        sn_by_detector={},
        jira_by_detector={},
        run_timestamp_iso=RUN_TS.isoformat(),
        source_payloads=[_java_block()],
    )
    assert "java_app" not in rd


# ─────────────────────────────────────────────────────────────────────────────
# AC7 — end-to-end: a discovery finding grounded in Java operational data
# ─────────────────────────────────────────────────────────────────────────────
def test_ac7_discovery_finding_grounded_in_java_operational_data(monkeypatch):
    """Ingest the real Java operational surface, build the corroboration block,
    and confirm a finding's confidence is elevated on the strength of it."""
    monkeypatch.setenv("INGEST_MODE", "offline")
    monkeypatch.setattr("app.telemetry.record_event", lambda *a, **k: None)

    # 1. Ingest the operational surface (Actuator samples + logs) of the
    #    configured Java apps through the change runner.
    collected: list = []
    store = {}
    change_runner.ingest_with_checkpoint(
        JavaAppIngestor(), "org-1",
        process_batch=lambda b: collected.extend(b.records),
        read_checkpoint=lambda o, c: store.get((o, c)),
        save_checkpoint=lambda cp: store.__setitem__((cp.org_id, cp.connector_id), cp),
    )
    assert collected, "expected operational records from the Java application"

    # 2. Build the corroboration block from the ingested operational signal.
    payload = build_java_app_corroboration_payload(collected)
    assert payload["java_app"]["operational_friction"]["fired"] is True

    # 3. A discovery run with the Java app connected alongside another system
    #    produces a finding whose confidence is elevated by the Java signal —
    #    discovery across a non-SaaS enterprise source.
    rd = build_corroboration_run_data(
        systems={"salesforce", "java_app"},
        sn_by_detector={},
        jira_by_detector={},
        run_timestamp_iso=RUN_TS.isoformat(),
        source_payloads=[payload],
    )
    result = evaluate_corroboration(DETECTOR, "service_cloud", rd, RUN_TS, "org-1")
    assert "COR-09" in result.rule_ids
    assert result.elevated_confidence == "HIGH"
    assert apply_corroboration_confidence("MEDIUM", result) == "HIGH"
