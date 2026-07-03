"""
R17-A4 / T2 + T4 — .NET application operational signal extraction (thin adapter).

The .NET counterpart to :mod:`discovery.ingest.java_app_signals`. It deliberately
duplicates NONE of the extraction logic: error clustering, latency-degradation
detection, throughput decline, resource pressure, and the run-level friction
rollup are the SHARED :mod:`discovery.ingest.operational_signals`, reused verbatim
(R17-A4 / AC3, "share the extraction, not just the idea"). This module only binds
the .NET identity: ``source_system='dotnet_app'`` on the provenance pointer and the
``'dotnet_app'`` corroboration key the engine's COR-10 recognises.

Because the extraction is shared, AgentIQ explains Java and .NET runtime friction
in exactly the same signal language, so downstream scoring, corroboration, and
reporting need no per-platform logic.

Phase one reads the OPERATIONAL surface only (AC8) — what the running application
reports about itself — and never the application's source code (the separate 1.8
code-and-structure phase).
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, Optional

from .operational_signals import build_evidence_pointer as _build_evidence_pointer
from .operational_signals import (
    build_operational_corroboration_payload,
    build_operational_signal,
)

#: The connector's own source identity. Provenance pointers and the corroboration
#: block are both keyed off this exact system id (the engine keys COR-10 off it).
DOTNET_APP_SOURCE_SYSTEM = "dotnet_app"
DOTNET_APP_CORROBORATION_KEY = "dotnet_app"


def build_evidence_pointer(
    app_id: str,
    artifact_kind: str,
    artifact_ref: str,
    source_timestamp: Optional[str],
) -> Dict[str, Any]:
    """Build the OBSERVED EvidencePointer for one .NET-app operational signal (T4/AC5).

    .NET-bound convenience over :func:`operational_signals.build_evidence_pointer`
    with ``source_system='dotnet_app'`` — operational signals are directly
    measured, so the pointer is OBSERVED (never inferred) and needs no
    ``extraction_job_id``.
    """
    return _build_evidence_pointer(
        DOTNET_APP_SOURCE_SYSTEM, app_id, artifact_kind, artifact_ref, source_timestamp
    )


def build_dotnet_app_signal(records: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Aggregate .NET-app operational records into the downstream signal block.

    Thin alias for the shared :func:`operational_signals.build_operational_signal`;
    the rollup shape is identical for Java and .NET.
    """
    return build_operational_signal(records)


def build_dotnet_app_corroboration_payload(
    records: Iterable[Dict[str, Any]],
) -> Dict[str, Any]:
    """Package .NET-app signal into the corroboration-engine input block (T5/AC6).

    Wraps the shared signal under the ``'dotnet_app'`` key that the corroboration
    engine's ``_find_corroboration_block('dotnet_app', …)`` recognises. The engine
    reads the ``operational_friction`` block to fire COR-10 — a .NET-app operational
    signal corroborating a finding in another connected system elevates confidence,
    exactly as Java's COR-09 does, because operational signals are first-class
    observed evidence (R17-A4 §3). This only *feeds* the signal in the engine's
    shape — elevation is the engine's job.
    """
    return build_operational_corroboration_payload(records, DOTNET_APP_CORROBORATION_KEY)
