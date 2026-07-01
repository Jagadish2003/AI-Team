"""
R17-A4 / T4 + T5 — .NET application operational signal → corroboration shaping.

This module is the heart of "feed .NET operational signals into the existing
corroboration flow" (R17-A4 §3). It does two things and nothing more:

1. **Provenance (T4 / AC5).** :func:`build_evidence_pointer` stamps every .NET
   operational signal with a fully-populated, OBSERVED
   :class:`~app.provenance.EvidencePointer` — ``source_system='dotnet_app'``,
   the application/endpoint artifact id, a timestamp, and ``origin='observed'``.
   Because the signal is read directly from the running application's logs and
   diagnostics, it is **first-class observed evidence, never inferred**.

2. **Corroboration shaping (T5 / AC6).**
   :func:`build_dotnet_app_corroboration_payload` packages the signal into the
   exact block the cross-system corroboration engine already consumes for other
   enterprise sources, keyed under ``'dotnet_app'`` — the same
   ``operational_friction`` shape the Java source uses for COR-09. The engine
   reads it to fire **COR-10**, so a .NET runtime signal (rising errors / latency,
   resource pressure, a recurring exception cluster) can *support and strengthen*
   a finding that already exists in another connected system (e.g. a ServiceNow
   incident spike for the same service).

No separate .NET confidence model
---------------------------------
This deliberately introduces **no new confidence mechanism**. The .NET signal is
shaped to plug into the *same* corroboration approach used by ServiceNow, Jira,
and the Java source: it only *feeds* the signal; whether (and how much) it
elevates a finding is decided by the corroboration engine. The interpretation of
the raw operational readings into a friction signal is **reused from the Java
ingestor** (:func:`java_app_signals.build_java_app_signal`, whose output block is
platform-agnostic) rather than re-implemented — the two enterprise-application
sources speak the same signal language, so downstream scoring/corroboration need
no per-technology logic.

The corroboration-ready signal shape (what the engine can understand)
---------------------------------------------------------------------
The payload the engine reads carries everything it needs to reason about the
signal: the **source system** (``dotnet_app`` — the payload key and each record's
``source_system``/evidence pointer), **application identity** (per-service rollup,
keyed by ``service``/``app_id``), **signal type** (the friction ``reasons`` — error
rate, latency, throughput, resource pressure, exception cluster), a **timestamp**
(``operational_friction.timestamp``, windowed by the engine), **confidence-related
data** (``fired`` plus the per-service ``metrics`` gauges), and **provenance** (the
OBSERVED evidence pointer on every underlying record).
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

from app.provenance import EvidencePointer, utc_now_iso

# The Java ingestor already turns operational records into the platform-agnostic
# ``{operational_friction, services}`` block. Reuse it rather than duplicating the
# extraction — only the identity (source_system / corroboration key) is .NET-bound.
from discovery.ingest.java_app_signals import build_java_app_signal as _build_operational_signal

#: The connector's own source identity. Provenance pointers and the corroboration
#: block are both keyed off this exact system id — the engine keys COR-10 off it,
#: so the block MUST be fed under this key.
DOTNET_APP_SOURCE_SYSTEM = "dotnet_app"
DOTNET_APP_CORROBORATION_KEY = "dotnet_app"


def build_evidence_pointer(
    app_id: str,
    artifact_kind: str,
    artifact_ref: str,
    source_timestamp: Optional[str],
) -> Dict[str, Any]:
    """Build the R16-B1 OBSERVED EvidencePointer for one .NET-app signal (T4 / AC5).

    Every operational signal must be traceable back to the exact source artifact it
    was measured from, so each record carries a fully-populated, OBSERVED provenance
    pointer:

      * ``source_system`` = ``'dotnet_app'``.
      * ``source_artifact`` = ``"{app_id}:{artifact_kind}:{artifact_ref}"`` — the
        application id plus the surface (``metrics`` | ``log``) and the sample time
        / log offset that uniquely identify the reading. Stable, so
        ``source_artifact_type`` is ``'record_id'``.
      * ``source_timestamp`` = the artifact's own observation time (UTC ISO); falls
        back to now only when missing, so the mandatory spine is always populated.
      * ``origin`` = ``'observed'`` — measured directly from the running
        application's logs/diagnostics, never inferred, so no ``extraction_job_id``
        is required. This is what lets a .NET signal count as first-class support
        for a related finding.
    """
    return EvidencePointer.observed(
        source_system=DOTNET_APP_SOURCE_SYSTEM,
        source_artifact=f"{app_id}:{artifact_kind}:{artifact_ref}",
        source_timestamp=source_timestamp or utc_now_iso(),
        source_artifact_type="record_id",
    ).to_dict()


#: .NET log levels that the reused Java extractor does not know as "errors". The
#: base extractor counts ERROR/FATAL/SEVERE; .NET emits ``Critical`` for its most
#: severe level, so it is aliased to a level the extractor recognises (for counting
#: ONLY — the stored record keeps its accurate .NET ``CRITICAL`` level).
_EXTRACTOR_LEVEL_ALIAS: Dict[str, str] = {"CRITICAL": "ERROR"}


def _as_extractable(record: Dict[str, Any]) -> Dict[str, Any]:
    """Present a .NET record in the field/level vocabulary the reused extractor reads.

    Two tiny, non-destructive bridges keep .NET records clean while reusing the exact
    same friction interpretation as Java (no separate extraction, no separate
    confidence model):

      * metrics — the .NET ingestor emits neutral resource-gauge fields
        (``memory_used_ratio`` / ``cpu_usage``); the reused extractor reads
        ``jvm_memory_used_ratio`` / ``system_cpu_usage``, so those are aliased.
      * logs — .NET's most severe level ``CRITICAL`` is aliased to ``ERROR`` so the
        reused extractor (which knows ERROR/FATAL/SEVERE) counts it as an error and
        can cluster it; the stored record keeps its accurate ``CRITICAL`` level.

    Returns a COPY used only for extraction — the persisted record is untouched.
    """
    kind = record.get("artifact_kind")
    if kind == "metrics":
        return {
            **record,
            "jvm_memory_used_ratio": record.get("memory_used_ratio"),
            "system_cpu_usage": record.get("cpu_usage"),
        }
    if kind == "log":
        level = str(record.get("level", "")).strip().upper()
        if level in _EXTRACTOR_LEVEL_ALIAS:
            return {**record, "level": _EXTRACTOR_LEVEL_ALIAS[level]}
    return record


def build_dotnet_app_signal(records: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Aggregate .NET-app operational records into the downstream signal block.

    Reuses the platform-agnostic Java aggregator so the output shape
    (``{operational_friction, services}``) is identical to the Java source's —
    the two enterprise-application sources produce the same signal language.
    """
    return _build_operational_signal([_as_extractable(r) for r in records])


def build_dotnet_app_corroboration_payload(
    records: Iterable[Dict[str, Any]],
) -> Dict[str, Any]:
    """Package .NET-app signal into the corroboration-engine input block (T5 / AC6).

    Wraps :func:`build_dotnet_app_signal` under the ``'dotnet_app'`` key that the
    corroboration engine's ``_find_corroboration_block('dotnet_app', …)``
    recognises. The engine reads the ``operational_friction`` block to fire
    **COR-10** — a .NET-app operational signal corroborating a finding in another
    connected system (e.g. a .NET error-rate/latency rise corroborating a ServiceNow
    incident spike for the same service) elevates confidence, because operational
    signals are first-class observed evidence (R17-A4 §3).

    This function only *feeds* the signal in the shape the engine consumes — it
    attaches no confidence and performs no elevation; that is the engine's job. It
    reuses the same cross-system corroboration approach as every other enterprise
    source (no separate .NET confidence model).
    """
    records = list(records)
    return {DOTNET_APP_CORROBORATION_KEY: build_dotnet_app_signal(records)}
