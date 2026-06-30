"""
R17-A3 / T4 — provenance for Java application operational signals (R16-B1).

Every Java-application operational signal must say WHERE it came from, WHEN it
was observed, and WHICH artifact or endpoint produced it. This module is the one
place that builds that provenance, and it does so by REUSING the Evidence &
Identity Spine (:class:`app.provenance.EvidencePointer`) — the Java ingestor must
not invent a separate provenance model (doc Section 3).

Three properties hold for every Java-app signal (AC4):

  * ``source_system == 'java_app'`` — fixed, so downstream systems can tell Java
    operational evidence apart from Salesforce / ServiceNow / Jira / Slack /
    GitHub / database evidence.
  * ``origin == 'observed'`` — the signal is directly measured runtime behaviour
    (a log entry, a metric/health sample), never guessed or inferred. Observed
    pointers carry no ``extraction_job_id`` and validate without one.
  * a meaningful, stable ``source_artifact`` — for a log it points at the log
    position / event in the stream; for a diagnostics sample it points at the
    target application + endpoint + sample time. Either way a Java-grounded
    finding can be traced back to the exact operational source that supported it.

The builders return JSON-serialisable dicts (``EvidencePointer.to_dict()``) ready
to attach to a signal record's ``evidence_pointer`` field.
"""
from __future__ import annotations

from typing import Optional

try:  # spine lives in the app package; tests run with backend/ on sys.path
    from app.provenance import OBSERVED, EvidencePointer
except ModuleNotFoundError:  # pragma: no cover - repo-root import fallback
    from backend.app.provenance import OBSERVED, EvidencePointer  # type: ignore

#: The fixed source-system tag for every Java-application signal. Distinguishes
#: Java operational evidence from every other connector's evidence.
SOURCE_SYSTEM = "java_app"

#: source_artifact stability guarantee passed to the spine: a Java-app artifact id
#: is a stable id that resolves back to the same operational source.
_ARTIFACT_TYPE = "record_id"


def log_artifact_id(
    app_id: str, *, log_offset: Optional[int] = None, event_id: Optional[str] = None
) -> str:
    """A stable artifact reference for a LOG signal.

    Per the doc this may be a log stream/position, an event id, or a generated
    artifact reference. A native event id is preferred when present (it is the
    most precise handle on the exact log event); otherwise the (application, log
    position) pair pins the exact place in the log stream the signal came from.
    """
    if event_id:
        return f"{app_id}:log:event:{event_id}"
    return f"{app_id}:log:{int(log_offset or 0)}"


def actuator_artifact_id(
    app_id: str, observed_at: str, *, metric_name: Optional[str] = None
) -> str:
    """A stable artifact reference for a diagnostics-ENDPOINT (Actuator) signal.

    Per the doc this may be the endpoint, the metric name, the sample time, or the
    target application id. It combines the target application, the actuator
    endpoint, the sampled metric (when the signal is per-metric) and the sample
    time, so the reference resolves to the exact endpoint sample that produced it.
    """
    if metric_name:
        return f"{app_id}:actuator:{metric_name}:{observed_at}"
    return f"{app_id}:actuator:{observed_at}"


def build_evidence_pointer(
    *, source_artifact: str, source_timestamp: Optional[str]
) -> dict:
    """Build the OBSERVED :class:`EvidencePointer` (R16-B1) for a Java-app signal.

    ``source_system`` is fixed to ``'java_app'`` and ``origin`` to ``'observed'``
    (so no ``extraction_job_id`` is needed). ``source_timestamp`` is the moment the
    signal was observed (the log/sample time); when missing it defaults to now so
    the mandatory spine is always populated and the pointer always validates.
    Returned as a JSON-serialisable dict for the record's ``evidence_pointer``.
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
    """Observed provenance for one log signal (convenience over the two builders)."""
    return build_evidence_pointer(
        source_artifact=log_artifact_id(app_id, log_offset=log_offset, event_id=event_id),
        source_timestamp=source_timestamp,
    )


def build_actuator_evidence_pointer(
    app_id: str,
    observed_at: str,
    *,
    metric_name: Optional[str] = None,
) -> dict:
    """Observed provenance for one diagnostics-endpoint signal."""
    return build_evidence_pointer(
        source_artifact=actuator_artifact_id(app_id, observed_at, metric_name=metric_name),
        source_timestamp=observed_at,
    )
