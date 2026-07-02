"""
R17-A4 / T4 — provenance for .NET application operational signals (R16-B1).

Every .NET operational signal must say WHERE it came from, WHEN it was observed,
and WHICH artifact/endpoint produced it, so a .NET-supported finding can be traced
back to the exact operational evidence behind it. This module is the one place
that builds that provenance, and it does so by REUSING the Evidence & Identity
Spine (:class:`app.provenance.EvidencePointer`) — the .NET ingestor must not
create a separate provenance format just for this connector (doc Section 3).

Three properties hold for every .NET-app signal (AC5):

  * ``source_system == 'dotnet_app'`` — fixed, so downstream systems can tell .NET
    operational evidence apart from Salesforce / ServiceNow / Jira / Slack /
    GitHub / Java / database evidence.
  * ``origin == 'observed'`` — the signal is directly measured runtime behaviour
    (a diagnostics/metric sample, a log event), never inferred or guessed.
    Observed pointers carry no ``extraction_job_id`` and validate without one.
  * a meaningful, stable ``source_artifact`` — a stable reference to the observed
    operational event: a log entry / log-stream position (or a native event id),
    or an endpoint / metric sample (target app + surface + sample time), or an
    application instance. Either way the reference resolves back to the reading.

The builders return JSON-serialisable dicts (``EvidencePointer.to_dict()``) ready
to attach to a signal record's ``evidence_pointer`` field.
"""
from __future__ import annotations

from typing import Optional

try:  # spine lives in the app package; tests run with backend/ on sys.path
    from app.provenance import OBSERVED, EvidencePointer
except ModuleNotFoundError:  # pragma: no cover - repo-root import fallback
    from backend.app.provenance import OBSERVED, EvidencePointer  # type: ignore

#: The fixed source-system tag for every .NET-application signal.
SOURCE_SYSTEM = "dotnet_app"

#: source_artifact stability guarantee passed to the spine: a .NET-app artifact id
#: is a stable id that resolves back to the same observed operational event.
_ARTIFACT_TYPE = "record_id"


def log_artifact_id(
    app_id: str, *, log_offset: Optional[int] = None, event_id: Optional[str] = None
) -> str:
    """A stable artifact reference for a LOG signal.

    A native log event id is preferred when present (the most precise handle on
    the exact log event); otherwise the (application, log-stream position) pair
    pins the exact place in the log stream the signal came from.
    """
    if event_id:
        return f"{app_id}:log:event:{event_id}"
    return f"{app_id}:log:{int(log_offset or 0)}"


def metric_artifact_id(
    app_id: str,
    sample_ts: str,
    *,
    metric_name: Optional[str] = None,
    seq_index: int = 0,
) -> str:
    """A stable artifact reference for a diagnostics-ENDPOINT / metric sample.

    Combines the target application, the diagnostics surface, the sampled metric
    (when the signal is per-metric) and the sample time, so the reference resolves
    to the exact endpoint sample that produced it. ``seq_index`` disambiguates
    samples that share a timestamp so their ids stay unique.
    """
    ref = sample_ts if seq_index <= 0 else f"{sample_ts}:{seq_index}"
    if metric_name:
        return f"{app_id}:metrics:{metric_name}:{ref}"
    return f"{app_id}:metrics:{ref}"


def instance_artifact_id(app_id: str, instance_id: str) -> str:
    """A stable artifact reference for an application-INSTANCE-level signal."""
    return f"{app_id}:instance:{instance_id}"


def build_evidence_pointer(
    *, source_artifact: str, source_timestamp: Optional[str]
) -> dict:
    """Build the OBSERVED :class:`EvidencePointer` (R16-B1) for a .NET-app signal.

    ``source_system`` is fixed to ``'dotnet_app'`` and ``origin`` to
    ``'observed'`` (so no ``extraction_job_id`` is needed). ``source_timestamp`` is
    the moment the signal was observed (the sample/log time); when missing it
    defaults to now so the mandatory spine is always populated and the pointer
    always validates. Returned as a JSON-serialisable dict.
    """
    return EvidencePointer.observed(
        source_system=SOURCE_SYSTEM,
        source_artifact=source_artifact,
        source_timestamp=source_timestamp or None,
        source_artifact_type=_ARTIFACT_TYPE,
    ).to_dict()


def build_log_evidence_pointer(
    app_id: str,
    *,
    log_offset: Optional[int] = None,
    event_id: Optional[str] = None,
    source_timestamp: Optional[str] = None,
) -> dict:
    """Observed provenance for one .NET log signal."""
    return build_evidence_pointer(
        source_artifact=log_artifact_id(app_id, log_offset=log_offset, event_id=event_id),
        source_timestamp=source_timestamp,
    )


def build_metric_evidence_pointer(
    app_id: str,
    sample_ts: str,
    *,
    metric_name: Optional[str] = None,
    seq_index: int = 0,
) -> dict:
    """Observed provenance for one .NET diagnostics/metric sample signal."""
    return build_evidence_pointer(
        source_artifact=metric_artifact_id(
            app_id, sample_ts, metric_name=metric_name, seq_index=seq_index
        ),
        source_timestamp=sample_ts,
    )
