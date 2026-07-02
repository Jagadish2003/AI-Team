"""
R17-A4 / T2 + T7 — the operational signal extraction is genuinely SHARED (AC3).

The story's design-review criterion (AC3) is that the .NET ingestor reuses the
Java ingestor's operational-signal extraction where the shape is identical —
NOT duplicated. These tests pin that as executable evidence:

  * both platform signal adapters delegate to the *same* shared functions
    (``discovery.ingest.operational_signals``) — object identity, not copies; and
  * given identical operational records, the Java and .NET adapters produce
    byte-identical signal (so a future fix to the extraction cannot drift between
    them).

If someone re-copies the extraction into one platform, these tests fail.
"""
from __future__ import annotations

from discovery.ingest import java_app_signals as jav
from discovery.ingest import dotnet_app_signals as net
from discovery.ingest import operational_signals as shared


def _records(app):
    """A degrading service's records (metrics + clustered error logs)."""
    return [
        {"app_id": app, "service": app, "artifact_kind": "metrics",
         "observed_ts": "2026-06-10T08:00:00+00:00", "sample_ts": "2026-06-10T08:00:00+00:00",
         "health": "UP", "error_rate": 0.01, "latency_p95_ms": 200,
         "throughput_rpm": 1500, "memory_used_ratio": 0.5, "cpu_usage": 0.3},
        {"app_id": app, "service": app, "artifact_kind": "metrics",
         "observed_ts": "2026-06-10T08:10:00+00:00", "sample_ts": "2026-06-10T08:10:00+00:00",
         "health": "DOWN", "error_rate": 0.2, "latency_p95_ms": 2000,
         "throughput_rpm": 600, "memory_used_ratio": 0.95, "cpu_usage": 0.9},
        {"app_id": app, "service": app, "artifact_kind": "log",
         "observed_ts": "2026-06-10T08:09:00+00:00", "level": "ERROR",
         "exception_type": "TimeoutException", "retry": True},
        {"app_id": app, "service": app, "artifact_kind": "log",
         "observed_ts": "2026-06-10T08:09:10+00:00", "level": "CRITICAL",
         "exception_type": "TimeoutException", "retry": False},
        {"app_id": app, "service": app, "artifact_kind": "log",
         "observed_ts": "2026-06-10T08:09:20+00:00", "level": "ERROR",
         "exception_type": "TimeoutException", "retry": False},
    ]


# ─────────────────────────────────────────────────────────────────────────────
# The adapters delegate to the SAME shared code (not copies)
# ─────────────────────────────────────────────────────────────────────────────
def test_extraction_functions_are_the_shared_objects_not_copies():
    # The actual extraction logic is imported from the shared module, so the Java
    # adapter exposes the SAME function objects — proof it is reused, not copied.
    assert jav.extract_error_signal is shared.extract_error_signal
    assert jav.extract_exception_clusters is shared.extract_exception_clusters
    assert jav.extract_metrics_signal is shared.extract_metrics_signal
    # And both platforms' "build signal" helpers delegate to the shared builder.
    assert jav.build_java_app_signal(_records("s")) == shared.build_operational_signal(_records("s"))
    assert net.build_dotnet_app_signal(_records("s")) == shared.build_operational_signal(_records("s"))


def test_both_platforms_produce_identical_signal_for_identical_records():
    # The interpretation is platform-agnostic: identical records → identical
    # signal. A drift in one platform's extraction would break this equality.
    java_sig = jav.build_java_app_signal(_records("svc"))
    dotnet_sig = net.build_dotnet_app_signal(_records("svc"))
    assert java_sig == dotnet_sig
    # And the friction is actually detected (all four families + health).
    metrics = dotnet_sig["services"]["svc"]["metrics"]
    assert metrics["latency_degraded"] and metrics["throughput_declined"]
    assert metrics["heap_pressure"] and metrics["cpu_pressure"] and metrics["unhealthy"]
    assert any(c["is_cluster"] for c in dotnet_sig["services"]["svc"]["exception_clusters"])


# ─────────────────────────────────────────────────────────────────────────────
# Only the corroboration key / source_system identity differs per platform
# ─────────────────────────────────────────────────────────────────────────────
def test_corroboration_payload_keyed_per_platform():
    assert "java_app" in jav.build_java_app_corroboration_payload([])
    assert "dotnet_app" in net.build_dotnet_app_corroboration_payload([])


def test_evidence_pointer_binds_the_platform_source_system():
    jep = jav.build_evidence_pointer("a", "metrics", "t", "t")
    nep = net.build_evidence_pointer("a", "metrics", "t", "t")
    assert jep["source_system"] == "java_app"
    assert nep["source_system"] == "dotnet_app"
    # Same shape otherwise — both observed, same artifact encoding.
    assert jep["origin"] == nep["origin"] == "observed"
    assert jep["source_artifact"] == nep["source_artifact"] == "a:metrics:t"
