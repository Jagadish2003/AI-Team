"""Telemetry event registry and fail-silent recording helpers."""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Mapping, MutableMapping, Optional, Type, TypedDict


logger = logging.getLogger(__name__)

EVENT_REGISTRY: MutableMapping[str, Type[Any]] = {}


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
    pack_id: str
    signal_count: int
    detector_count: int
    fired_count: int
    below_threshold: int


class RunSignalSnapshotEvent(TypedDict):
    org_id: str
    run_id: str
    source: str
    success: bool
    count: int
    payload: RunSignalSnapshotPayload


def record_event(
    event_type: Optional[str] = None,
    payload: Optional[Mapping[str, Any]] = None,
    *,
    org_id: Optional[str] = None,
    source: Optional[str] = None,
    run_id: Optional[str] = None,
    success: Optional[bool] = None,
    count: Optional[int] = None,
    ts: Optional[float] = None,
    **extra: Any,
) -> None:
    """Record a telemetry event as structured log data.

    This function intentionally never raises. It supports both the generic
    call shape used by earlier telemetry work, ``record_event(type, payload)``,
    and the keyword-rich shape used by temporal snapshot capture.
    """
    try:
        if not event_type:
            logger.debug("[telemetry] record_event called without event_type")
            return None

        payload_dict = dict(payload or {})
        temporal_metadata = {
            "org_id": org_id,
            "source": source,
            "run_id": run_id,
            "success": success,
            "count": count,
        }
        has_temporal_metadata = any(value is not None for value in temporal_metadata.values())

        if has_temporal_metadata:
            event_payload: Dict[str, Any] = {
                key: value
                for key, value in temporal_metadata.items()
                if value is not None
            }
            event_payload["payload"] = payload_dict
        else:
            event_payload = payload_dict

        event_payload.update(extra)

        event = {
            "event_type": event_type,
            "ts": ts if ts is not None else time.time(),
            **event_payload,
        }

        if logger.isEnabledFor(logging.DEBUG) and event_type not in EVENT_REGISTRY:
            logger.debug(
                "[telemetry] record_event called with unregistered type '%s'",
                event_type,
            )

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
register_event_type("run.signal_snapshot", RunSignalSnapshotEvent)


__all__ = [
    "ConnectorRegisteredEvent",
    "DbIngestorCompletedEvent",
    "DbQueryExecutedEvent",
    "EVENT_REGISTRY",
    "RunCompletedEvent",
    "RunSignalSnapshotEvent",
    "RunSignalSnapshotPayload",
    "RunStartedEvent",
    "record_event",
    "register_event_type",
]
