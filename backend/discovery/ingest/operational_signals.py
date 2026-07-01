"""
R17-A4 / T2 — Shared operational signal extraction (platform-agnostic).

The *interpretation* of runtime friction is the same whether the running
application is a Java (R17-A3) or a .NET (R17-A4) enterprise application: an
error-rate rise, latency degradation, a throughput drop, resource pressure, and a
recurring exception cluster mean the same thing to downstream scoring,
corroboration, and reporting regardless of which platform emitted them. Only the
*collection* differs — Java exposes Spring Boot Actuator + JVM gauges, .NET
exposes ASP.NET Core health checks + EventCounters — so this module owns the
common extraction ONCE and each ingestor feeds it a normalised operational
reading (R17-A4 §2, Architectural Note "Share the extraction, not just the idea";
AC3).

The four operational signal types (R17-A3 §1 / R17-A4 §1)
---------------------------------------------------------
1. **Error patterns / error-rate** — error and exception volume over the logs and
   the platform-reported request error rate. See :func:`extract_error_signal`.
2. **Latency / throughput degradation** — rising request latency and falling
   throughput across the sampled metrics. See :func:`extract_metrics_signal`.
3. **Exception clustering** — recurring exceptions grouped by type, the reach-
   phase proxy for a systemic fault. See :func:`extract_exception_clusters`.
4. **Resource pressure** — heap (JVM heap / .NET managed GC heap) and CPU
   pressure reported by the platform. See :func:`extract_metrics_signal`.

The normalised reading (the collection ↔ extraction contract)
-------------------------------------------------------------
Each platform's COLLECTION layer maps its native surface to these
platform-neutral fields before handing records here — so the extraction never
needs to know whether a memory ratio came from ``jvm.memory.used`` (Java) or the
``gc-heap-size`` EventCounter (.NET):

  * metric samples: ``sample_ts``, ``health``, ``error_rate``,
    ``latency_p95_ms``, ``throughput_rpm``, ``memory_used_ratio``, ``cpu_usage``.
  * log records: ``level`` (canonical upper-case), ``exception_type``, ``retry``.

Why this is operational, not source (AC8)
------------------------------------------
Every input here is something the *running* application reports about itself —
health state, request metrics, resource gauges, and the lines it writes to its
own log. None of it reads the application's source code, class structure, or
configuration files. That distinction is the operational/code boundary phase one
draws against the separate 1.8 code-and-structure phase.

Provenance (R16-B1 / AC4/AC5)
-----------------------------
Every record an ingestor produces carries a fully-populated OBSERVED
:class:`~app.provenance.EvidencePointer` built by :func:`build_evidence_pointer`
(``origin='observed'`` — operational signals are directly measured, so they are
first-class observed evidence, never inferred). The ``source_system`` is supplied
by the calling platform (``'java_app'`` / ``'dotnet_app'``) so the pointer traces
back to the exact source.

Downstream shape (corroboration)
--------------------------------
:func:`build_operational_corroboration_payload` aggregates the records into the
block the corroboration engine reads (an ``operational_friction`` block —
{fired, timestamp, services, reasons} — plus the per-service rollup) and keys it
under the platform's corroboration key. An operational signal corroborating a
finding in another system (e.g. an error-rate rise corroborating a ServiceNow
incident spike for the same service) elevates confidence exactly as cross-system
corroboration does elsewhere — identically for Java and .NET, because both speak
the same signal language.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Dict, Iterable, List, Optional

from app.provenance import EvidencePointer, utc_now_iso

# ─────────────────────────────────────────────────────────────────────────────
# Friction thresholds (reach-phase proxies for "runtime friction")
# ─────────────────────────────────────────────────────────────────────────────
#: Request error rate (errors / total) at or above which a sample is "degraded".
ERROR_RATE_THRESHOLD = 0.05
#: p95 request latency (ms) at or above which a sample shows latency degradation.
LATENCY_P95_THRESHOLD_MS = 1000.0
#: Heap used-ratio at or above which a sample shows memory pressure. Applies to
#: both the JVM heap and the .NET managed GC heap — both are garbage-collected
#: heaps, so "heap pressure" is a genuinely shared concept.
HEAP_PRESSURE_THRESHOLD = 0.85
#: System CPU usage (0..1) at or above which a sample shows CPU pressure.
CPU_PRESSURE_THRESHOLD = 0.85
#: Number of occurrences of one exception type at/above which it is a "cluster".
EXCEPTION_CLUSTER_MIN = 3
#: Log levels treated as errors for error-pattern counting. A deliberately
#: cross-platform vocabulary: Java/log4j/JUL use ERROR/FATAL/SEVERE; .NET's
#: ``Microsoft.Extensions.Logging`` uses Error/Critical. Each collection layer
#: normalises its native level to one of these upper-case tokens (AC8: level
#: strings only, never message-content interpretation).
_ERROR_LEVELS = {"ERROR", "FATAL", "SEVERE", "CRITICAL"}


# ─────────────────────────────────────────────────────────────────────────────
# Provenance (R16-B1 / AC4/AC5)
# ─────────────────────────────────────────────────────────────────────────────

def build_evidence_pointer(
    source_system: str,
    app_id: str,
    artifact_kind: str,
    artifact_ref: str,
    source_timestamp: Optional[str],
) -> Dict[str, Any]:
    """Build the R16-B1 EvidencePointer for one operational signal.

    Every operational signal must be traceable back to the exact source artifact
    it was measured from, so each record carries a fully-populated, OBSERVED
    provenance pointer:

      * ``source_system`` = the calling platform's id (``'java_app'`` /
        ``'dotnet_app'``).
      * ``source_artifact`` = ``"{app_id}:{artifact_kind}:{artifact_ref}"`` — the
        application id plus the surface (``metrics`` | ``log``) and the sample
        time / log offset that uniquely identify the reading. Stable, so
        ``source_artifact_type`` is ``'record_id'``.
      * ``source_timestamp`` = the artifact's own observation time (UTC ISO);
        falls back to now only when missing, so the mandatory spine is always
        populated.
      * ``origin`` = ``'observed'`` — measured directly from the running
        application, never inferred, so no ``extraction_job_id`` is required.
    """
    return EvidencePointer.observed(
        source_system=source_system,
        source_artifact=f"{app_id}:{artifact_kind}:{artifact_ref}",
        source_timestamp=source_timestamp or utc_now_iso(),
        source_artifact_type="record_id",
    ).to_dict()


# ─────────────────────────────────────────────────────────────────────────────
# Signal extraction (reach-phase: counts, rates, gauges — never source code)
# ─────────────────────────────────────────────────────────────────────────────

def _as_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def extract_error_signal(log_records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Extract error-pattern signal from a service's log records.

    Counts error/fatal log lines and retry/failure markers and computes the error
    share of the logs. Pure counts over structured log fields (``level``,
    ``exception_type``) — the message text is not interpreted (AC8).
    """
    total = len(log_records)
    error_lines = [
        r for r in log_records
        if str(r.get("level", "")).strip().upper() in _ERROR_LEVELS
    ]
    retry_failures = sum(
        1 for r in log_records if bool(r.get("retry") or r.get("is_retry_failure"))
    )
    error_count = len(error_lines)
    return {
        "log_count": total,
        "error_count": error_count,
        "retry_failure_count": retry_failures,
        "error_share": round(error_count / total, 4) if total else 0.0,
    }


def extract_exception_clusters(log_records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Group error log records by exception type to surface recurring faults.

    A recurring exception (``count >= EXCEPTION_CLUSTER_MIN``) is the reach-phase
    proxy for a systemic fault. Returns clusters sorted by count desc, then type,
    so the output is deterministic. Structured grouping by ``exception_type`` —
    no message-content NLP (AC8).
    """
    counter: Counter = Counter()
    for r in log_records:
        if str(r.get("level", "")).strip().upper() not in _ERROR_LEVELS:
            continue
        exc = str(r.get("exception_type", "")).strip()
        if exc:
            counter[exc] += 1
    clusters = [
        {"exception_type": exc, "count": count, "is_cluster": count >= EXCEPTION_CLUSTER_MIN}
        for exc, count in counter.items()
    ]
    clusters.sort(key=lambda c: (-c["count"], c["exception_type"]))
    return clusters


def extract_metrics_signal(metric_records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Extract latency/throughput/error-rate/resource-pressure signal from samples.

    Reads the normalised metric samples (health, request metrics, heap/CPU gauges)
    and derives the operational behaviour of the running application: peak error
    rate and p95 latency, latency degradation (rising p95 across the window),
    throughput decline, and resource pressure (heap/CPU). All values are
    counts/rates/gauges the application reports about itself (AC8) — the collection
    layer has already mapped its platform-native surface onto the neutral
    ``memory_used_ratio`` / ``cpu_usage`` fields read here.
    """
    if not metric_records:
        return {
            "sample_count": 0,
            "max_error_rate": 0.0,
            "max_latency_p95_ms": 0.0,
            "latency_degraded": False,
            "throughput_declined": False,
            "heap_pressure": False,
            "cpu_pressure": False,
            "unhealthy": False,
        }

    ordered = sorted(metric_records, key=lambda m: str(m.get("sample_ts", "")))

    error_rates = [v for v in (_as_float(m.get("error_rate")) for m in ordered) if v is not None]
    latencies = [v for v in (_as_float(m.get("latency_p95_ms")) for m in ordered) if v is not None]
    throughputs = [v for v in (_as_float(m.get("throughput_rpm")) for m in ordered) if v is not None]
    heaps = [v for v in (_as_float(m.get("memory_used_ratio")) for m in ordered) if v is not None]
    cpus = [v for v in (_as_float(m.get("cpu_usage")) for m in ordered) if v is not None]

    max_error_rate = max(error_rates) if error_rates else 0.0
    max_latency = max(latencies) if latencies else 0.0
    # Degradation: the latest p95 is materially worse than the earliest one and
    # over the threshold (a sustained rise, not a single blip).
    latency_degraded = bool(
        len(latencies) >= 2
        and latencies[-1] >= LATENCY_P95_THRESHOLD_MS
        and latencies[-1] > latencies[0]
    )
    throughput_declined = bool(
        len(throughputs) >= 2 and throughputs[-1] < throughputs[0]
    )
    heap_pressure = bool(heaps and max(heaps) >= HEAP_PRESSURE_THRESHOLD)
    cpu_pressure = bool(cpus and max(cpus) >= CPU_PRESSURE_THRESHOLD)
    unhealthy = any(
        str(m.get("health", "")).strip().upper() not in ("", "UP", "HEALTHY")
        for m in ordered
    )

    return {
        "sample_count": len(ordered),
        "max_error_rate": round(max_error_rate, 4),
        "max_latency_p95_ms": round(max_latency, 2),
        "latency_degraded": latency_degraded,
        "throughput_declined": throughput_declined,
        "heap_pressure": heap_pressure,
        "cpu_pressure": cpu_pressure,
        "unhealthy": unhealthy,
    }


def _friction_reasons(error: Dict[str, Any], metrics: Dict[str, Any], clusters: List[Dict[str, Any]]) -> List[str]:
    """Human-readable reasons a service is showing operational friction.

    Each reason corresponds to one of the Section 1 friction patterns. The list
    is the audit trail behind ``operational_friction.fired`` — and it is the same
    vocabulary for Java and .NET, so downstream reporting needs no per-platform
    wording.
    """
    reasons: List[str] = []
    if metrics.get("max_error_rate", 0.0) >= ERROR_RATE_THRESHOLD or error.get("error_count", 0) > 0:
        reasons.append("elevated error rate")
    if metrics.get("latency_degraded"):
        reasons.append("latency degradation")
    if metrics.get("throughput_declined"):
        reasons.append("throughput decline")
    if metrics.get("heap_pressure") or metrics.get("cpu_pressure"):
        reasons.append("resource pressure")
    if metrics.get("unhealthy"):
        reasons.append("unhealthy health check")
    if any(c.get("is_cluster") for c in clusters):
        reasons.append("recurring exception cluster")
    return reasons


def build_service_signal(service: str, records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Build the operational signal rollup for one service from its records.

    Splits the records into metric samples and log entries by their
    ``artifact_kind``, extracts each signal type, and decides whether the service
    is showing runtime friction. ``timestamp`` is the freshest observation in the
    window so the corroboration engine can window it (R17-A3 §3 / R17-A4 §3).
    """
    metric_records = [r for r in records if r.get("artifact_kind") == "metrics"]
    log_records = [r for r in records if r.get("artifact_kind") == "log"]

    error = extract_error_signal(log_records)
    clusters = extract_exception_clusters(log_records)
    metrics = extract_metrics_signal(metric_records)
    reasons = _friction_reasons(error, metrics, clusters)

    timestamps = [str(r.get("observed_ts", "")) for r in records if r.get("observed_ts")]
    latest_ts = max(timestamps) if timestamps else None

    return {
        "service": service,
        "fired": bool(reasons),
        "timestamp": latest_ts,
        "reasons": reasons,
        "error_patterns": error,
        "exception_clusters": clusters,
        "metrics": metrics,
    }


def build_operational_signal(records: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Aggregate operational records into the downstream signal block.

    Groups records by service and produces a run-level ``operational_friction``
    block — ``fired`` is True when any service shows friction — plus the per-
    service rollups and the list of services with friction. This is the payload
    passed downstream; whether it elevates a finding's confidence is decided by
    the corroboration engine. Platform-agnostic: the same rollup shape is produced
    for Java and .NET.
    """
    records = list(records)

    by_service: Dict[str, List[Dict[str, Any]]] = {}
    for r in records:
        svc = str(r.get("service") or r.get("app_id") or "")
        by_service.setdefault(svc, []).append(r)

    services = {svc: build_service_signal(svc, recs) for svc, recs in sorted(by_service.items())}
    fired_services = sorted(svc for svc, sig in services.items() if sig["fired"])

    fired_timestamps = [
        services[svc]["timestamp"] for svc in fired_services if services[svc]["timestamp"]
    ]
    latest_ts = max(fired_timestamps) if fired_timestamps else None

    operational_friction = {
        "fired": bool(fired_services),
        "timestamp": latest_ts,
        "services": fired_services,
        "reasons": sorted({reason for svc in fired_services for reason in services[svc]["reasons"]}),
    }

    return {
        "operational_friction": operational_friction,
        "services": services,
    }


def build_operational_corroboration_payload(
    records: Iterable[Dict[str, Any]],
    corroboration_key: str,
) -> Dict[str, Any]:
    """Package operational signal into the corroboration-engine input block.

    Wraps :func:`build_operational_signal` under ``corroboration_key`` — the exact
    system id the corroboration engine's ``_find_corroboration_block(key, …)``
    recognises (``'java_app'`` → COR-09, ``'dotnet_app'`` → COR-10). The engine
    reads the ``operational_friction`` block to fire the rule; an operational
    signal corroborating a finding in another connected system elevates confidence,
    because operational signals are first-class observed evidence.

    This function only *feeds* the signal in the shape the engine consumes — it
    attaches no confidence and performs no elevation; that is the engine's job.
    """
    return {corroboration_key: build_operational_signal(records)}
