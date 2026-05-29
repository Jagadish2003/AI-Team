"""T1-S10-C - Telemetry event registry and non-blocking event recorder.

Track 1 owns this module. Track 3 (T3-S10-A-TS09) registers the
run.signal_snapshot event type and RunSignalSnapshotPayload here.
record_event() is designed to accept any registered event type without
modification when new types are added to TELEMETRY_EVENT_REGISTRY.
"""
from __future__ import annotations

import logging
from typing import Any, Optional, TypedDict

logger = logging.getLogger(__name__)


class RunSignalSnapshotPayload(TypedDict):
    metric_key: str
    value: float
    baseline: Optional[float]


# Canonical registry mapping event type strings to their payload TypedDict class.
# Add new event types here; record_event() validates against this dict without
# needing modification, satisfying the T1-S10-C no-modification contract.
TELEMETRY_EVENT_REGISTRY: dict[str, type] = {
    "run.signal_snapshot": RunSignalSnapshotPayload,
}


def record_event(event_type: str, payload: dict[str, Any]) -> None:
    """Non-blocking telemetry emitter. Never raises.

    Unregistered event types are logged as warnings and dropped so that
    callers cannot silently emit unrecognized telemetry.
    """
    try:
        if event_type not in TELEMETRY_EVENT_REGISTRY:
            logger.warning(
                "telemetry: unregistered event type %r - dropped", event_type
            )
            return
        logger.info("telemetry event type=%r payload=%r", event_type, payload)
    except Exception:
        logger.exception("telemetry: record_event suppressed exception")


__all__ = [
    "TELEMETRY_EVENT_REGISTRY",
    "RunSignalSnapshotPayload",
    "record_event",
]
