"""
R17-A3 / T2 + T4 — Java application operational signal extraction (thin adapter).

The *interpretation* of Java operational data into SIGNAL is not Java-specific:
error clustering, latency-degradation detection, throughput decline, resource
pressure, and the run-level friction rollup are common to every operational
enterprise source, so they live in :mod:`discovery.ingest.operational_signals`
and are shared verbatim with the .NET ingestor (R17-A4 / AC3). This module is the
Java-flavoured adapter over that shared core: it binds ``source_system='java_app'``
onto the provenance pointer and keys the corroboration payload under ``'java_app'``
(the id the corroboration engine's COR-09 recognises). Nothing here duplicates the
extraction logic — only the Java identity is bound.

Phase one reads the OPERATIONAL surface only (AC8) — what the running application
reports about itself — and never the application's source code (the separate 1.8
code-and-structure phase).
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

# Re-export the shared extraction so existing importers of this module keep
# working unchanged (the functions themselves are platform-agnostic now).
from .operational_signals import (  # noqa: F401
    CPU_PRESSURE_THRESHOLD,
    ERROR_RATE_THRESHOLD,
    EXCEPTION_CLUSTER_MIN,
    HEAP_PRESSURE_THRESHOLD,
    LATENCY_P95_THRESHOLD_MS,
    build_operational_signal,
    build_service_signal,
    extract_error_signal,
    extract_exception_clusters,
    extract_metrics_signal,
)
from .operational_signals import build_evidence_pointer as _build_evidence_pointer
from .operational_signals import build_operational_corroboration_payload

#: The connector's own source identity. Provenance pointers and the corroboration
#: block are both keyed off this exact system id (the engine keys COR-09 off it).
JAVA_APP_SOURCE_SYSTEM = "java_app"
JAVA_APP_CORROBORATION_KEY = "java_app"


def build_evidence_pointer(
    app_id: str,
    artifact_kind: str,
    artifact_ref: str,
    source_timestamp: Optional[str],
) -> Dict[str, Any]:
    """Build the OBSERVED EvidencePointer for one Java-app operational signal (T4/AC4).

    Java-bound convenience over :func:`operational_signals.build_evidence_pointer`
    with ``source_system='java_app'`` — operational signals are directly measured,
    so the pointer is OBSERVED (never inferred) and needs no ``extraction_job_id``.
    """
    return _build_evidence_pointer(
        JAVA_APP_SOURCE_SYSTEM, app_id, artifact_kind, artifact_ref, source_timestamp
    )


def build_java_app_signal(records: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Aggregate Java-app operational records into the downstream signal block.

    Thin alias for the shared :func:`operational_signals.build_operational_signal`;
    the rollup shape is identical for Java and .NET.
    """
    return build_operational_signal(records)


def build_java_app_corroboration_payload(
    records: Iterable[Dict[str, Any]],
) -> Dict[str, Any]:
    """Package Java-app signal into the corroboration-engine input block (T5/AC5).

    Wraps the shared signal under the ``'java_app'`` key that the corroboration
    engine's ``_find_corroboration_block('java_app', …)`` recognises. The engine
    reads the ``operational_friction`` block to fire COR-09 — a Java-app operational
    signal corroborating a finding in another connected system (e.g. an error-rate
    rise corroborating a ServiceNow incident spike for the same service) elevates
    confidence, because operational signals are first-class observed evidence
    (R17-A3 §3). This only *feeds* the signal in the engine's shape — elevation is
    the engine's job.
    """
    return build_operational_corroboration_payload(records, JAVA_APP_CORROBORATION_KEY)
