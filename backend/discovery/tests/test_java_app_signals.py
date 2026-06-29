"""
R17-A3 / T2 — tests for Java application operational signal extraction.

Covers the acceptance criteria assigned to this subtask:

  AC1 — The ingestor produces operational SIGNAL from a configured Java
        application's health/diagnostics endpoints and logs (here: the extraction
        layer turns log entries + Actuator samples into structured signal, never
        raw text).
  AC4 — Every signal carries a valid EvidencePointer with
        source_system='java_app', an artifact id, a timestamp, and
        origin='observed'.
  AC8 — Operational surface only: the module consumes logs and metric samples,
        never source code.

The four signal families are tested individually as pure functions, then end to
end through ``build_java_app_signal`` / ``build_java_app_corroboration_payload``.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from app.provenance import EvidencePointer
from discovery.ingest.java_app_signals import (
    JAVA_APP_CORROBORATION_KEY,
    JAVA_APP_SYSTEM,
    build_java_app_corroboration_payload,
    build_java_app_signal,
    cluster_exceptions,
    extract_degradation_signals,
    extract_error_patterns,
    extract_resource_pressure,
)

FIXTURE = Path(__file__).resolve().parents[1] / "ingest" / "fixtures" / "java_app_sample.json"


def _load_fixture() -> dict:
    with open(FIXTURE, encoding="utf-8") as fh:
        return json.load(fh)


def _by(records, key, value):
    return [r for r in records if r.get(key) == value]


# ─────────────────────────────────────────────────────────────────────────────
# Signal family 1 — error patterns from logs
# ─────────────────────────────────────────────────────────────────────────────
def test_recurring_error_message_becomes_pattern():
    logs = _load_fixture()["logs"]
    patterns = extract_error_patterns(logs, application_id="payments-service")
    persist = next(p for p in patterns if "persist payment record" in p["template"])
    # Variable record numbers collapse to one template seen 3x → recurring.
    assert persist["template"] == "Failed to persist payment record <n> to ledger"
    assert persist["count"] == 3
    assert "recurring" in persist["categories"]


def test_timeout_and_failed_downstream_overlap_with_target():
    patterns = extract_error_patterns(_load_fixture()["logs"], application_id="payments-service")
    timeout = next(p for p in patterns if "Read timed out" in p["template"])
    # The same line is a timeout AND a failed downstream call, and recurs 3x.
    assert {"timeout", "failed_downstream_call", "recurring"} <= set(timeout["categories"])
    assert timeout["downstream_target"] == "billing-api"
    assert timeout["count"] == 3


def test_retry_loop_detected():
    patterns = extract_error_patterns(_load_fixture()["logs"], application_id="payments-service")
    retry = next(p for p in patterns if "Retrying connection" in p["template"])
    assert retry["categories"] == ["retry_loop"]
    # Attempt numbers are masked so the two retry lines share one template.
    assert retry["template"] == "Retrying connection to inventory-service, attempt <n> of <n>"


def test_single_failed_downstream_call_with_5xx():
    patterns = extract_error_patterns(_load_fixture()["logs"], application_id="payments-service")
    ds = next(p for p in patterns if "fraud-check" in p["template"])
    assert ds["categories"] == ["failed_downstream_call"]
    assert ds["downstream_target"] == "fraud-check"


def test_benign_error_without_category_is_not_a_pattern():
    # A NullPointerException line carries no recurring/timeout/downstream/retry
    # marker, so it is NOT surfaced as an error *pattern* (it is an exception
    # cluster instead). Info-level lines are ignored entirely.
    patterns = extract_error_patterns(_load_fixture()["logs"], application_id="payments-service")
    templates = [p["template"] for p in patterns]
    assert not any("reconciliation" in t for t in templates)
    assert not any("Started PaymentService" in t for t in templates)


def test_error_patterns_sorted_by_count_desc():
    patterns = extract_error_patterns(_load_fixture()["logs"], application_id="payments-service")
    counts = [p["count"] for p in patterns]
    assert counts == sorted(counts, reverse=True)


# ─────────────────────────────────────────────────────────────────────────────
# Signal family 3 — exception clustering
# ─────────────────────────────────────────────────────────────────────────────
def test_exceptions_cluster_by_type_and_frame_ignoring_line_drift():
    clusters = cluster_exceptions(_load_fixture()["logs"], application_id="payments-service")
    timeouts = _by(clusters, "exception_type", "java.net.SocketTimeoutException")
    assert len(timeouts) == 1
    cluster = timeouts[0]
    # Three occurrences, two at line 88 and one at line 90 — line number dropped
    # from the signature so they all cluster together.
    assert cluster["count"] == 3
    assert cluster["recurring"] is True
    assert cluster["top_frame"].startswith("com.acme.payments.HttpClient.call")


def test_npe_cluster_grouped():
    clusters = cluster_exceptions(_load_fixture()["logs"], application_id="payments-service")
    npe = _by(clusters, "exception_type", "java.lang.NullPointerException")
    assert len(npe) == 1
    assert npe[0]["count"] == 2
    assert npe[0]["recurring"] is True


def test_exception_type_extracted_from_message_without_stack():
    clusters = cluster_exceptions(_load_fixture()["logs"], application_id="payments-service")
    ise = _by(clusters, "exception_type", "java.lang.IllegalStateException")
    assert len(ise) == 1
    assert ise[0]["count"] == 1
    assert ise[0]["recurring"] is False
    assert ise[0]["top_frame"] is None


# ─────────────────────────────────────────────────────────────────────────────
# Signal family 2 — latency / throughput / error-rate / health degradation
# ─────────────────────────────────────────────────────────────────────────────
def test_latency_degradation_detected():
    samples = _load_fixture()["metric_samples"]
    sig = extract_degradation_signals(samples, application_id="payments-service")
    lat = next(d for d in sig if d["kind"] == "latency_degradation")
    assert lat["direction"] == "up"
    assert lat["current_value"] > lat["baseline_value"]
    assert lat["change_pct"] >= 30.0


def test_throughput_degradation_detected():
    sig = extract_degradation_signals(_load_fixture()["metric_samples"], application_id="payments-service")
    tp = next(d for d in sig if d["kind"] == "throughput_degradation")
    assert tp["direction"] == "down"
    assert tp["current_value"] < tp["baseline_value"]


def test_error_rate_rise_detected():
    sig = extract_degradation_signals(_load_fixture()["metric_samples"], application_id="payments-service")
    er = next(d for d in sig if d["kind"] == "error_rate_rise")
    assert er["current_value"] > er["baseline_value"]


def test_service_unhealthy_detected():
    sig = extract_degradation_signals(_load_fixture()["metric_samples"], application_id="payments-service")
    health = next(d for d in sig if d["kind"] == "service_unhealthy")
    assert health["health"] == "DOWN"


def test_no_degradation_for_stable_service():
    stable = [
        {"service": "svc", "timestamp": "2026-06-29T10:00:00Z", "health": "UP", "latency_p95_ms": 100, "throughput_rpm": 5000, "error_rate": 0.001},
        {"service": "svc", "timestamp": "2026-06-29T10:05:00Z", "health": "UP", "latency_p95_ms": 102, "throughput_rpm": 4950, "error_rate": 0.001},
    ]
    assert extract_degradation_signals(stable) == []


# ─────────────────────────────────────────────────────────────────────────────
# Signal family 4 — resource pressure
# ─────────────────────────────────────────────────────────────────────────────
def test_all_resource_pressure_families_fire_on_latest_sample():
    pressure = extract_resource_pressure(_load_fixture()["metric_samples"], application_id="payments-service")
    kinds = {p["kind"] for p in pressure}
    assert kinds == {"memory_pressure", "cpu_pressure", "thread_pool_pressure", "queue_pressure"}
    mem = next(p for p in pressure if p["kind"] == "memory_pressure")
    assert mem["utilization"] >= mem["threshold"]


def test_resource_pressure_skipped_when_metric_absent():
    # Only CPU exposed and it is healthy → no pressure at all.
    samples = [{"service": "svc", "timestamp": "2026-06-29T10:00:00Z", "cpu_usage": 0.10}]
    assert extract_resource_pressure(samples) == []


# ─────────────────────────────────────────────────────────────────────────────
# AC4 — every signal carries a valid OBSERVED evidence pointer
# ─────────────────────────────────────────────────────────────────────────────
def test_every_signal_has_valid_observed_evidence_pointer():
    fx = _load_fixture()
    signal = build_java_app_signal(
        log_entries=fx["logs"], metric_samples=fx["metric_samples"], application_id="payments-service"
    )
    all_signals = (
        signal["error_patterns"]
        + signal["exception_clusters"]
        + signal["degradations"]
        + signal["resource_pressure"]
    )
    assert all_signals  # there is signal to check
    for s in all_signals:
        ptr = EvidencePointer.from_dict(s["evidence_pointer"])
        assert ptr.is_valid()
        assert ptr.source_system == JAVA_APP_SYSTEM
        assert ptr.origin == "observed"
        assert ptr.extraction_job_id is None  # observed evidence needs no job id
        assert ptr.source_artifact
        # timestamp parses as ISO-8601.
        datetime.fromisoformat(ptr.source_timestamp.replace("Z", "+00:00"))


# ─────────────────────────────────────────────────────────────────────────────
# AC1 — the aggregate produces structured operational signal end to end
# ─────────────────────────────────────────────────────────────────────────────
def test_build_java_app_signal_summary():
    fx = _load_fixture()
    signal = build_java_app_signal(
        log_entries=fx["logs"], metric_samples=fx["metric_samples"], application_id="payments-service"
    )
    summary = signal["summary"]
    assert summary["has_friction"] is True
    assert summary["error_pattern_count"] == 4
    assert summary["degradation_count"] == 4
    assert summary["resource_pressure_count"] == 4
    assert summary["services"] == ["payments-service"]


def test_build_java_app_signal_empty_inputs():
    signal = build_java_app_signal(log_entries=[], metric_samples=[])
    assert signal["summary"]["has_friction"] is False
    assert signal["error_patterns"] == []
    assert signal["degradations"] == []


def test_idle_application_yields_no_signal():
    # An idle app (no error logs, healthy stable metrics) yields a minimal,
    # friction-free signal — the extraction analogue of an empty delta (AC2).
    idle_logs = [{"timestamp": "2026-06-29T10:00:00Z", "level": "INFO", "message": "ok"}]
    idle_metrics = [{"service": "svc", "timestamp": "2026-06-29T10:00:00Z", "health": "UP", "latency_p95_ms": 50, "throughput_rpm": 100, "error_rate": 0.0, "cpu_usage": 0.1}]
    signal = build_java_app_signal(log_entries=idle_logs, metric_samples=idle_metrics)
    assert signal["summary"]["has_friction"] is False


# ─────────────────────────────────────────────────────────────────────────────
# Corroboration readiness (feeds T5 / AC5) — observed signal in a consumable shape
# ─────────────────────────────────────────────────────────────────────────────
def test_corroboration_payload_shape_and_markers():
    fx = _load_fixture()
    payload = build_java_app_corroboration_payload(
        log_entries=fx["logs"], metric_samples=fx["metric_samples"], application_id="payments-service"
    )
    assert JAVA_APP_CORROBORATION_KEY in payload
    block = payload[JAVA_APP_CORROBORATION_KEY]
    markers = block["corroboration_markers"]
    # The error-rate rise corroborates e.g. a ServiceNow incident spike, with a
    # recognisable service + timestamp the corroboration engine can window.
    assert markers["error_rate_rise"]["fired"] is True
    assert markers["error_rate_rise"]["services"] == ["payments-service"]
    assert isinstance(markers["error_rate_rise"]["timestamp"], str)
    assert markers["service_unhealthy"]["fired"] is True
    assert markers["recurring_exceptions"]["fired"] is True
    assert markers["friction"]["fired"] is True


def test_determinism_same_input_same_output():
    fx = _load_fixture()
    a = build_java_app_signal(log_entries=fx["logs"], metric_samples=fx["metric_samples"])
    b = build_java_app_signal(log_entries=fx["logs"], metric_samples=fx["metric_samples"])
    assert a == b
