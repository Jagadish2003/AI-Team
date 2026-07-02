"""
R17-A4 / T1 + T2 (AC3) — .NET reuses the SHARED operational-signal extraction.

The signal-extraction logic (error/exception clustering, latency-degradation and
throughput-decline detection, resource-pressure flags) is identical between Java
and .NET, so it is genuinely shared code — :mod:`discovery.ingest.operational_signals`
is reused by BOTH platform adapters, NOT duplicated (R17-A4 §2, Architectural Note
"Share the extraction, not just the idea"). These tests prove the .NET ingestor's
signal is produced by the same extractor objects the Java ingestor uses, and that
.NET's collection-layer normalisation feeds it correctly.
"""
from __future__ import annotations

import pytest

from discovery.ingest import dotnet_app_signals, java_app_signals, operational_signals
from discovery.ingest.dotnet_app import DotNetAppIngestor
from discovery.ingest.dotnet_app_signals import build_dotnet_app_signal


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    monkeypatch.setenv("INGEST_MODE", "offline")


def _records(since=None):
    return [r for b in DotNetAppIngestor().ingest_changes("org1", since) for r in b.records]


def test_extraction_is_reused_not_duplicated():
    # Both platform adapters delegate to the SAME shared extraction module — the
    # same function objects, so the extraction is genuinely shared, not copied.
    assert dotnet_app_signals.build_operational_signal is operational_signals.build_operational_signal
    assert java_app_signals.build_operational_signal is operational_signals.build_operational_signal
    records = _records()
    assert build_dotnet_app_signal(records) == java_app_signals.build_java_app_signal(records)


def test_shared_extraction_fires_friction_for_the_degraded_dotnet_service():
    signal = build_dotnet_app_signal(_records())
    orders = signal["services"]["orders"]
    assert orders["fired"] is True
    reasons = set(orders["reasons"])
    # Each Section-1 friction pattern, derived by the SHARED extractor from the
    # .NET readings normalised by this ingestor's collection layer.
    assert {"elevated error rate", "latency degradation", "throughput decline",
            "resource pressure", "unhealthy health check",
            "recurring exception cluster"} <= reasons


def test_shared_extraction_clusters_dotnet_exceptions():
    signal = build_dotnet_app_signal(_records())
    clusters = signal["services"]["orders"]["exception_clusters"]
    timeout = next(c for c in clusters if c["exception_type"] == "System.TimeoutException")
    assert timeout["count"] == 3 and timeout["is_cluster"] is True


def test_healthy_dotnet_service_shows_no_friction():
    signal = build_dotnet_app_signal(_records())
    assert signal["services"]["inventory"]["fired"] is False


def test_collection_layer_normalises_dotnet_readings_to_canonical_fields():
    # The .NET readings are mapped onto the platform-neutral fields the shared
    # extractor consumes (memory_used_ratio for the managed GC heap, etc.).
    metric = next(r for r in _records() if r["artifact_kind"] == "metrics")
    assert "error_rate" in metric and "memory_used_ratio" in metric
    assert "cpu_usage" in metric and "throughput_rpm" in metric


def test_collection_layer_normalises_dotnet_log_levels():
    # .NET LogLevel spellings (Error/Critical/Warning/Information) are normalised
    # onto the shared canonical vocabulary so the shared error counting reads them
    # identically to Java levels — this mapping is the .NET collection edge.
    logs = [r for r in _records() if r["artifact_kind"] == "log"]
    levels = {r["level"] for r in logs}
    assert levels <= {"TRACE", "DEBUG", "INFO", "WARN", "ERROR", "CRITICAL", "FATAL", ""}
    assert "CRITICAL" in levels          # fixture's 'Critical' entry, normalised
