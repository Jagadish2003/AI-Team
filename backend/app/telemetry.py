"""Telemetry event registry and fail-silent recording helpers."""

from __future__ import annotations

import logging
import time
from typing import Any, MutableMapping, Optional, Type, TypedDict


logger = logging.getLogger(__name__)

EVENT_REGISTRY: MutableMapping[str, Type[Any]] = {}
TELEMETRY_EVENT_REGISTRY = EVENT_REGISTRY


def register_event_type(event_type: str, schema: Type[Any]) -> None:
    """Register an event type and payload schema.

    Registering the same event type with the same schema is idempotent.
    Registering it with a different schema raises so ownership mistakes are
    caught during development.
    """
    if event_type in EVENT_REGISTRY:
        if EVENT_REGISTRY[event_type] is not schema:
            raise ValueError(
                f"Telemetry event type '{event_type}' is already registered "
                f"with {EVENT_REGISTRY[event_type]!r}; cannot re-register "
                f"with {schema!r}."
            )
        return

    EVENT_REGISTRY[event_type] = schema


class RunStartedEvent(TypedDict):
    run_id: str
    org_id: str


class RunCompletedEvent(TypedDict):
    run_id: str
    org_id: str
    duration_ms: int
    connectors_processed: int


class ConnectorRegisteredEvent(TypedDict):
    connector_id: str
    org_id: str


class DbQueryExecutedEvent(TypedDict):
    connector_id: str
    query_hash: str
    row_count: int
    duration_ms: int
    driver: str
    truncated: bool


class DbIngestorCompletedEvent(TypedDict):
    connector_id: str
    tables_processed: int
    rows_ingested: int
    duration_ms: int


class RunSignalSnapshotPayload(TypedDict):
    metric_key: str
    value: float
    baseline: Optional[float]


RunSignalSnapshotEvent = RunSignalSnapshotPayload


def record_event(event_type: str, payload: dict[str, Any]) -> None:
    """Record a telemetry event as structured log data.

    This function intentionally never raises. Track 3 uses the generic
    two-argument shape for run.signal_snapshot.
    """
    try:
        if event_type not in EVENT_REGISTRY:
            logger.warning(
                "[telemetry] unregistered event type '%s' - dropped", event_type
            )
            return None

        event = {
            "event_type": event_type,
            "ts": time.time(),
            **dict(payload),
        }

        logger.info("[telemetry] %s", event)
    except Exception:  # noqa: BLE001 - telemetry must never disrupt callers.
        try:
            logger.exception(
                "[telemetry] record_event failed silently for event_type='%s'",
                event_type,
            )
        except Exception:
            return None

    return None


register_event_type("run.started", RunStartedEvent)
register_event_type("run.completed", RunCompletedEvent)
register_event_type("connector.registered", ConnectorRegisteredEvent)
register_event_type("db.query_executed", DbQueryExecutedEvent)
register_event_type("db.ingestor_completed", DbIngestorCompletedEvent)
register_event_type("run.signal_snapshot", RunSignalSnapshotPayload)


__all__ = [
    "ConnectorRegisteredEvent",
    "DbIngestorCompletedEvent",
    "DbQueryExecutedEvent",
    "EVENT_REGISTRY",
    "RunCompletedEvent",
    "RunSignalSnapshotEvent",
    "RunSignalSnapshotPayload",
    "RunStartedEvent",
    "TELEMETRY_EVENT_REGISTRY",
    "record_event",
    "register_event_type",
]
