"""Azure export loaders for the Event-History Bridge — MSP-B8 / T3.

Turn standard Azure event-export files into rows in the MSP-B8 staging schema
(``ops_event_staging``), so the bridge ingestor (T4) can later map each raw
payload through the MSP-B0 mappers. V1 scope mirrors the AWS loaders (T2) and the
B1/B2 native-connector event classes (alarms/alerts, state changes, audit):

  * ``load_azure_monitor_alerts`` — Azure Monitor alert export.
    ``source_format='azure_monitor'``.
  * ``load_azure_activity_log`` — Azure Activity Log export.
    ``source_format='azure_activity_log'``.

V1 reads only the standard Azure export JSON shapes; bespoke SIEM re-exports and
tenant-specific transformations are out of scope.

All parsing / loud-skip / idempotency / batch-registry behaviour is shared with
the AWS loaders via :mod:`discovery.ingest.ops_event_loaders_common`; this module
supplies only the Azure edges — the container key, the ``provider_event_id``
extractor, and the ``event_time`` extractor. Each loader normalises JUST the
staging metadata the bridge needs (a dedupe id + an event timestamp) and keeps the
raw payload intact — it does NOT implement detector-facing event mapping. Final
mapping to the normalised model is the B0 mapper invocation in the bridge (T4).

Envelope shapes handled (Azure exposes alerts in two standard shapes):

  * **Common alert schema** — ``{"schemaId": "...", "data": {"essentials": {...}}}``
  * **Alerts Management API resource** — ``{"id", "name", "properties": {"essentials": {...}}}``,
    usually inside a ``{"value": [...]}`` list response.

Activity Log entries are the standard Activity Log records
(``eventDataId`` / ``eventTimestamp`` / ``operationName`` ...), as a bare list or a
``{"value": [...]}`` list response.
"""
from __future__ import annotations

import logging
from typing import Optional

from discovery.ingest.ops_event_loaders_common import (
    MALFORMED_JSON,
    MISSING_EVENT_ID,
    NOT_AN_OBJECT,
    UNREADABLE_FILE,
    LoadResult,
    SkippedRecord,
    Source,
    bounded_event_id,
    default_batch_id,
    dig,
    iter_json_container,
    parse_timestamp,
    resolve_sink,
    run_load,
    source_name,
)
from discovery.ingest.ops_event_staging_store import StagingSink

__all__ = [
    "PROVIDER_AZURE",
    "SOURCE_FORMAT_AZURE_MONITOR",
    "SOURCE_FORMAT_AZURE_ACTIVITY_LOG",
    "MALFORMED_JSON",
    "NOT_AN_OBJECT",
    "MISSING_EVENT_ID",
    "UNREADABLE_FILE",
    "SkippedRecord",
    "LoadResult",
    "load_azure_monitor_alerts",
    "load_azure_activity_log",
]

logger = logging.getLogger(__name__)

PROVIDER_AZURE = "azure"

SOURCE_FORMAT_AZURE_MONITOR = "azure_monitor"
SOURCE_FORMAT_AZURE_ACTIVITY_LOG = "azure_activity_log"


# ---------------------------------------------------------------------------
# provider_event_id + event_time extractors (per format)
# ---------------------------------------------------------------------------


def _azure_monitor_event_id(raw: dict) -> Optional[str]:
    """Alert identity across both envelope shapes.

    Prefers the alert resource ``id``, then the common-alert-schema
    ``data.essentials.alertId``, then the Alerts-API ``properties.essentials.alertId``.
    """
    eid = dig(
        raw,
        ("id",),
        ("data", "essentials", "alertId"),
        ("properties", "essentials", "alertId"),
    )
    return bounded_event_id(str(eid)) if eid else None


def _azure_monitor_event_time(raw: dict):
    """When the alert fired, across both envelope shapes."""
    ts = dig(
        raw,
        ("data", "essentials", "firedDateTime"),
        ("properties", "essentials", "startDateTime"),
        ("data", "essentials", "startDateTime"),
        ("properties", "essentials", "lastModifiedDateTime"),
    )
    return parse_timestamp(ts)


def _azure_activity_event_id(raw: dict) -> Optional[str]:
    """Activity Log record identity — ``eventDataId`` (unique per event), else ``id``."""
    eid = dig(raw, ("eventDataId",), ("id",), ("correlationId",))
    return bounded_event_id(str(eid)) if eid else None


def _azure_activity_event_time(raw: dict):
    ts = dig(raw, ("eventTimestamp",), ("submissionTimestamp",))
    return parse_timestamp(ts)


# ---------------------------------------------------------------------------
# Public loaders
# ---------------------------------------------------------------------------


def load_azure_monitor_alerts(
    source: Source,
    *,
    org_id: str,
    batch_id: Optional[str] = None,
    sink: Optional[StagingSink] = None,
) -> LoadResult:
    """Load an Azure Monitor alert export.

    Accepts a ``{"value": [...]}`` list response, a bare list of alerts,
    JSON-lines, a single alert (common alert schema or Alerts-API resource), a
    directory of such files, or an in-memory object. ``provider_event_id`` is the
    alert id; ``event_time`` is the fired/started time.
    """
    bid = batch_id or default_batch_id(PROVIDER_AZURE, SOURCE_FORMAT_AZURE_MONITOR, source)
    records = iter_json_container(
        source, container_keys=("value", "alerts"), file_globs=("*.json", "*.jsonl")
    )
    return run_load(
        records,
        org_id=org_id,
        provider=PROVIDER_AZURE,
        source_format=SOURCE_FORMAT_AZURE_MONITOR,
        batch_id=bid,
        source_reference=source_name(source),
        extract_event_id=_azure_monitor_event_id,
        extract_event_time=_azure_monitor_event_time,
        sink=resolve_sink(sink),
    )


def load_azure_activity_log(
    source: Source,
    *,
    org_id: str,
    batch_id: Optional[str] = None,
    sink: Optional[StagingSink] = None,
) -> LoadResult:
    """Load an Azure Activity Log export.

    Accepts a ``{"value": [...]}`` list response, a bare list of activity records,
    JSON-lines, a single record, a directory of such files, or an in-memory
    object. ``provider_event_id`` is the record ``eventDataId``; ``event_time`` is
    the record ``eventTimestamp``.
    """
    bid = batch_id or default_batch_id(PROVIDER_AZURE, SOURCE_FORMAT_AZURE_ACTIVITY_LOG, source)
    records = iter_json_container(
        source, container_keys=("value", "records"), file_globs=("*.json", "*.jsonl")
    )
    return run_load(
        records,
        org_id=org_id,
        provider=PROVIDER_AZURE,
        source_format=SOURCE_FORMAT_AZURE_ACTIVITY_LOG,
        batch_id=bid,
        source_reference=source_name(source),
        extract_event_id=_azure_activity_event_id,
        extract_event_time=_azure_activity_event_time,
        sink=resolve_sink(sink),
    )
