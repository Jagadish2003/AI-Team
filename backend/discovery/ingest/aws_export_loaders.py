"""AWS export loaders for the Event-History Bridge — MSP-B8 / T2.

Turn standard AWS event-export files into rows in the MSP-B8 staging schema
(``ops_event_staging``), so the bridge ingestor (T4) can later map each raw
payload through the MSP-B0 mappers. V1 scope, matching the B1/B2 native-connector
event classes (alarms/alerts, state changes, audit) — the bridge never claims
wider coverage than the native connectors will:

  * ``load_cloudwatch_alarm_history`` — CloudWatch alarm history
    (``DescribeAlarmHistory`` output). ``source_format='cloudwatch_alarm_history'``.
  * ``load_eventbridge_archive`` — EventBridge archive export (event envelopes).
    ``source_format='eventbridge_archive'``.
  * ``load_cloudtrail_logs`` — CloudTrail log files (``.json`` / ``.json.gz``,
    file or directory). ``source_format='cloudtrail'``.

Bespoke SIEM re-exports are out of scope (V1 reads the providers' standard
export/JSON formats only).

All parsing / loud-skip / idempotency / batch-registry behaviour is shared with
the Azure loaders in :mod:`discovery.ingest.ops_event_loaders_common`; this module
supplies only the AWS edges — the container key per format, the
``provider_event_id`` extractor, and the ``event_time`` extractor. Raw payloads
are preserved intact; nothing here does detector-facing mapping (that is the B0
mapper's job, invoked by the bridge in T4).
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
    iter_json_container,
    parse_timestamp,
    resolve_sink,
    run_load,
    source_name,
)
from discovery.ingest.ops_event_staging_store import StagingSink

# Re-exported so `MALFORMED_JSON`, `SkippedRecord`, etc. remain importable from
# this module (the T2 tests and any existing callers use them here).
__all__ = [
    "PROVIDER_AWS",
    "SOURCE_FORMAT_CLOUDWATCH",
    "SOURCE_FORMAT_EVENTBRIDGE",
    "SOURCE_FORMAT_CLOUDTRAIL",
    "MALFORMED_JSON",
    "NOT_AN_OBJECT",
    "MISSING_EVENT_ID",
    "UNREADABLE_FILE",
    "SkippedRecord",
    "LoadResult",
    "load_cloudwatch_alarm_history",
    "load_eventbridge_archive",
    "load_cloudtrail_logs",
]

logger = logging.getLogger(__name__)

PROVIDER_AWS = "aws"

SOURCE_FORMAT_CLOUDWATCH = "cloudwatch_alarm_history"
SOURCE_FORMAT_EVENTBRIDGE = "eventbridge_archive"
SOURCE_FORMAT_CLOUDTRAIL = "cloudtrail"


# ---------------------------------------------------------------------------
# provider_event_id + event_time extractors (per format)
# ---------------------------------------------------------------------------


def _cloudwatch_event_id(raw: dict) -> Optional[str]:
    """Composite id for a CloudWatch alarm-history item (no native event id).

    ``AlarmName | Timestamp | HistoryItemType`` (T1 doc §5.2). Requires at least
    an alarm name and a timestamp to be a stable identity.
    """
    name = raw.get("AlarmName")
    ts = raw.get("Timestamp")
    if not name or not ts:
        return None
    item_type = raw.get("HistoryItemType", "")
    return bounded_event_id(f"{name}|{ts}|{item_type}")


def _cloudwatch_event_time(raw: dict):
    return parse_timestamp(raw.get("Timestamp"))


def _eventbridge_event_id(raw: dict) -> Optional[str]:
    """EventBridge event envelope id (``id``)."""
    eid = raw.get("id")
    return bounded_event_id(str(eid)) if eid else None


def _eventbridge_event_time(raw: dict):
    return parse_timestamp(raw.get("time"))


def _cloudtrail_event_id(raw: dict) -> Optional[str]:
    """CloudTrail record id (``eventID``)."""
    eid = raw.get("eventID")
    return bounded_event_id(str(eid)) if eid else None


def _cloudtrail_event_time(raw: dict):
    return parse_timestamp(raw.get("eventTime"))


# ---------------------------------------------------------------------------
# Public loaders
# ---------------------------------------------------------------------------


def load_cloudwatch_alarm_history(
    source: Source,
    *,
    org_id: str,
    batch_id: Optional[str] = None,
    sink: Optional[StagingSink] = None,
) -> LoadResult:
    """Load a CloudWatch alarm-history export (``DescribeAlarmHistory`` output).

    Accepts the API's ``{"AlarmHistoryItems": [...]}`` container, a bare list of
    items, JSON-lines, a single item, a directory of such files, or an in-memory
    object. ``provider_event_id`` is a composite of alarm name + timestamp +
    history item type (no native id exists for alarm history); ``event_time`` is
    the item ``Timestamp``.
    """
    bid = batch_id or default_batch_id(PROVIDER_AWS, SOURCE_FORMAT_CLOUDWATCH, source)
    records = iter_json_container(
        source, container_keys=("AlarmHistoryItems",), file_globs=("*.json", "*.jsonl")
    )
    return run_load(
        records,
        org_id=org_id,
        provider=PROVIDER_AWS,
        source_format=SOURCE_FORMAT_CLOUDWATCH,
        batch_id=bid,
        source_reference=source_name(source),
        extract_event_id=_cloudwatch_event_id,
        extract_event_time=_cloudwatch_event_time,
        sink=resolve_sink(sink),
    )


def load_eventbridge_archive(
    source: Source,
    *,
    org_id: str,
    batch_id: Optional[str] = None,
    sink: Optional[StagingSink] = None,
) -> LoadResult:
    """Load an EventBridge archive export (event envelopes).

    Accepts a ``{"Events": [...]}`` container, a bare list of envelopes,
    JSON-lines, a single envelope, a directory of such files, or an in-memory
    object. ``provider_event_id`` is the envelope ``id``; ``event_time`` is the
    envelope ``time``.
    """
    bid = batch_id or default_batch_id(PROVIDER_AWS, SOURCE_FORMAT_EVENTBRIDGE, source)
    records = iter_json_container(
        source, container_keys=("Events", "events"), file_globs=("*.json", "*.jsonl")
    )
    return run_load(
        records,
        org_id=org_id,
        provider=PROVIDER_AWS,
        source_format=SOURCE_FORMAT_EVENTBRIDGE,
        batch_id=bid,
        source_reference=source_name(source),
        extract_event_id=_eventbridge_event_id,
        extract_event_time=_eventbridge_event_time,
        sink=resolve_sink(sink),
    )


def load_cloudtrail_logs(
    source: Source,
    *,
    org_id: str,
    batch_id: Optional[str] = None,
    sink: Optional[StagingSink] = None,
) -> LoadResult:
    """Load CloudTrail log files (``.json`` / ``.json.gz``, file or directory).

    Accepts the standard ``{"Records": [...]}`` log-file container (gzip
    transparently handled), a bare list, JSON-lines, a directory of log files, or
    an in-memory object. ``provider_event_id`` is the record ``eventID``;
    ``event_time`` is the record ``eventTime``.
    """
    bid = batch_id or default_batch_id(PROVIDER_AWS, SOURCE_FORMAT_CLOUDTRAIL, source)
    records = iter_json_container(
        source, container_keys=("Records",), file_globs=("*.json", "*.json.gz", "*.jsonl")
    )
    return run_load(
        records,
        org_id=org_id,
        provider=PROVIDER_AWS,
        source_format=SOURCE_FORMAT_CLOUDTRAIL,
        batch_id=bid,
        source_reference=source_name(source),
        extract_event_id=_cloudtrail_event_id,
        extract_event_time=_cloudtrail_event_time,
        sink=resolve_sink(sink),
    )
