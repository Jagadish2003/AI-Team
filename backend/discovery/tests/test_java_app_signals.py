"""
R17-A3 / T2 + T7 — Java application operational signal extraction.

Covers the four operational signal types the story's Section 1 calls for —
error patterns, latency/throughput degradation, exception clustering, and
resource pressure — and the run-level friction rollup that feeds corroboration.
Pure unit tests over the extraction functions; no DB, no I/O.
"""
from __future__ import annotations

from discovery.ingest.java_app_signals import (
    EXCEPTION_CLUSTER_MIN,
    build_java_app_corroboration_payload,
    build_java_app_signal,
    extract_error_signal,
    extract_exception_clusters,
    extract_metrics_signal,
)


def _log(level, exc=None, retry=False):
    return {"level": level, "exception_type": exc, "retry": retry, "message": "x"}


# ─────────────────────────────────────────────────────────────────────────────
# Error patterns
# ─────────────────────────────────────────────────────────────────────────────
def test_error_signal_counts_errors_and_retries():
    logs = [_log("INFO"), _log("ERROR", "TimeoutException"), _log("WARN", retry=True),
            _log("ERROR", "TimeoutException", retry=True)]
    sig = extract_error_signal(logs)
    assert sig["log_count"] == 4
    assert sig["error_count"] == 2
    assert sig["retry_failure_count"] == 2
    assert sig["error_share"] == 0.5


def test_error_signal_empty_logs():
    sig = extract_error_signal([])
    assert sig["error_count"] == 0
    assert sig["error_share"] == 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Exception clustering
# ─────────────────────────────────────────────────────────────────────────────
def test_exception_clusters_group_by_type_and_flag_recurring():
    logs = [_log("ERROR", "TimeoutException") for _ in range(EXCEPTION_CLUSTER_MIN)]
    logs += [_log("ERROR", "NullPointerException")]
    clusters = extract_exception_clusters(logs)
    by_type = {c["exception_type"]: c for c in clusters}
    assert by_type["TimeoutException"]["count"] == EXCEPTION_CLUSTER_MIN
    assert by_type["TimeoutException"]["is_cluster"] is True
    assert by_type["NullPointerException"]["is_cluster"] is False
    # Deterministic ordering: highest count first.
    assert clusters[0]["exception_type"] == "TimeoutException"


def test_exception_clusters_ignore_non_error_levels():
    logs = [_log("INFO", "TimeoutException"), _log("WARN", "TimeoutException")]
    assert extract_exception_clusters(logs) == []


# ─────────────────────────────────────────────────────────────────────────────
# Latency / throughput / resource pressure
# ─────────────────────────────────────────────────────────────────────────────
def _sample(ts, **kw):
    base = {"sample_ts": ts, "health": "UP", "error_rate": 0.0, "latency_p95_ms": 100,
            "throughput_rpm": 1000, "jvm_memory_used_ratio": 0.4, "system_cpu_usage": 0.3}
    base.update(kw)
    return base


def test_metrics_signal_detects_degradation_and_pressure():
    samples = [
        _sample("2026-06-10T08:00:00+00:00", latency_p95_ms=300, throughput_rpm=1200),
        _sample("2026-06-10T08:10:00+00:00", health="DOWN", error_rate=0.12,
                latency_p95_ms=1800, throughput_rpm=700,
                jvm_memory_used_ratio=0.91, system_cpu_usage=0.88),
    ]
    sig = extract_metrics_signal(samples)
    assert sig["max_error_rate"] == 0.12
    assert sig["latency_degraded"] is True           # 1800ms, rising from 300
    assert sig["throughput_declined"] is True         # 700 < 1200
    assert sig["heap_pressure"] is True               # 0.91 >= 0.85
    assert sig["cpu_pressure"] is True                # 0.88 >= 0.85
    assert sig["unhealthy"] is True                   # health DOWN


def test_metrics_signal_healthy_service_has_no_friction():
    samples = [_sample("2026-06-10T08:00:00+00:00"), _sample("2026-06-10T08:05:00+00:00", latency_p95_ms=110)]
    sig = extract_metrics_signal(samples)
    assert sig["latency_degraded"] is False
    assert sig["heap_pressure"] is False
    assert sig["cpu_pressure"] is False
    assert sig["unhealthy"] is False


def test_metrics_signal_empty():
    sig = extract_metrics_signal([])
    assert sig["sample_count"] == 0
    assert sig["latency_degraded"] is False


# ─────────────────────────────────────────────────────────────────────────────
# Run-level friction rollup + corroboration payload shape
# ─────────────────────────────────────────────────────────────────────────────
def _record(app, kind, ts, **kw):
    r = {"app_id": app, "service": app, "artifact_kind": kind, "observed_ts": ts}
    r.update(kw)
    return r


def test_build_signal_marks_only_friction_services():
    records = [
        # friction service: a degraded metric sample + clustered errors
        _record("payments", "metrics", "2026-06-10T08:10:00+00:00", health="DOWN",
                error_rate=0.2, latency_p95_ms=2000, throughput_rpm=100,
                jvm_memory_used_ratio=0.95, system_cpu_usage=0.9),
        _record("payments", "log", "2026-06-10T08:09:00+00:00", level="ERROR",
                exception_type="TimeoutException"),
        # quiet service: healthy sample, no errors
        _record("ledger", "metrics", "2026-06-10T08:00:00+00:00", health="UP",
                error_rate=0.0, latency_p95_ms=100, throughput_rpm=900,
                jvm_memory_used_ratio=0.3, system_cpu_usage=0.2),
    ]
    sig = build_java_app_signal(records)
    assert sig["operational_friction"]["fired"] is True
    assert sig["operational_friction"]["services"] == ["payments"]
    assert "ledger" not in sig["operational_friction"]["services"]
    assert sig["operational_friction"]["timestamp"] == "2026-06-10T08:10:00+00:00"


def test_corroboration_payload_is_keyed_under_java_app():
    payload = build_java_app_corroboration_payload([])
    assert "java_app" in payload
    assert payload["java_app"]["operational_friction"]["fired"] is False
