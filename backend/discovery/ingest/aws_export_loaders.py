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

Design guarantees (the T2 acceptance criteria):

  * **Raw payload preserved intact** — the provider record is stored verbatim in
    ``raw`` for downstream mapping and evidence resolution; nothing is stripped.
  * **Provider metadata for mapping + dedupe** — ``provider`` (``aws``) and
    ``source_format`` route the right B0 mapper; ``provider_event_id`` is the
    dedupe key extracted from the provider's own event identity.
  * **Loud-skip discipline** (the R18-A1 rule) — a malformed / unidentifiable
    record is skipped with a stable reason, counted, and logged at WARNING; valid
    records in the SAME batch still load. Nothing is silently dropped.
  * **Idempotent** at the batch and provider-event level — within one export a
    repeated event collapses to one row, and re-loading a batch inserts zero new
    rows (the sink enforces ``UNIQUE (org_id, provider, provider_event_id)``).

The loaders are pure over an injectable :class:`StagingSink`, so parsing and skip
logic are unit-testable with no database (see ``discovery/tests``). A ``source``
may be a filesystem path (file or directory) OR an already-parsed Python object
(dict/list) — the latter keeps tests file-free.
"""
from __future__ import annotations

import glob
import gzip
import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Iterable, List, Optional, Union

from database.models.ops_event_staging import (
    OpsEventLoadBatch,
    OpsEventStagingRow,
    _PROVIDER_EVENT_ID_LEN,
)
from discovery.ingest.ops_event_staging_store import DbStagingSink, StagingSink

logger = logging.getLogger(__name__)

PROVIDER_AWS = "aws"

SOURCE_FORMAT_CLOUDWATCH = "cloudwatch_alarm_history"
SOURCE_FORMAT_EVENTBRIDGE = "eventbridge_archive"
SOURCE_FORMAT_CLOUDTRAIL = "cloudtrail"

# ---------------------------------------------------------------------------
# Loud-skip vocabulary (stable tokens, surfaced with a WARNING and counted)
# ---------------------------------------------------------------------------

#: A record (or whole file/line) could not be parsed as JSON.
MALFORMED_JSON = "malformed_json"
#: A parsed record was not a JSON object (e.g. a bare string/number in an array).
NOT_AN_OBJECT = "not_an_object"
#: The record carried no extractable provider event identity — cannot dedupe it.
MISSING_EVENT_ID = "missing_event_id"
#: A file could not be read/opened at all.
UNREADABLE_FILE = "unreadable_file"

Source = Union[str, "os.PathLike[str]", dict, list]


@dataclass
class SkippedRecord:
    """One loud-skipped record — reason + safe detail, never the raw payload."""

    reason: str
    detail: str
    source_reference: Optional[str] = None
    index: Optional[int] = None


@dataclass
class LoadResult:
    """Outcome of one export load — what landed, what was skipped, and why."""

    org_id: str
    provider: str
    source_format: str
    batch_id: str
    source_reference: Optional[str] = None
    record_count: int = 0          # valid records parsed (the dedupe input)
    inserted_count: int = 0        # rows NEWLY written to staging this run
    duplicate_count: int = 0       # valid records that were already present
    skipped: List[SkippedRecord] = field(default_factory=list)

    @property
    def skipped_count(self) -> int:
        return len(self.skipped)

    def as_batch(self) -> OpsEventLoadBatch:
        return OpsEventLoadBatch(
            org_id=self.org_id,
            batch_id=self.batch_id,
            provider=self.provider,
            source_format=self.source_format,
            source_reference=self.source_reference,
            record_count=self.record_count,
            skipped_count=self.skipped_count,
        )


@dataclass
class _RawRecord:
    """A record yielded by a format reader: a parsed object OR a parse error."""

    source_reference: str
    index: int
    raw: Optional[dict] = None
    error: Optional[str] = None  # a skip reason token when the record is unusable


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bounded_event_id(value: str) -> str:
    """Return an id that fits ``provider_event_id``; hash if it would overflow.

    A composite id (CloudWatch has no native event id) can exceed the column
    width; hashing keeps it deterministic — the same input always yields the same
    id, so idempotency holds — while staying in bounds.
    """
    if len(value) <= _PROVIDER_EVENT_ID_LEN:
        return value
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _source_name(source: Source) -> str:
    if isinstance(source, (str, os.PathLike)):
        return os.path.basename(os.path.normpath(os.fspath(source))) or os.fspath(source)
    return "<memory>"


def _default_batch_id(source_format: str, source: Source) -> str:
    """Deterministic default batch id (T1 doc §5.1 convention).

    Stable across re-loads of the same source (no timestamp), so re-running a load
    reuses the batch id — aids auditing — while idempotency is guaranteed by
    ``provider_event_id`` regardless of the batch id.
    """
    return f"{PROVIDER_AWS}:{source_format}:{_source_name(source)}"


def _coerce_records(obj: Any, container_keys: Iterable[str]) -> Optional[list]:
    """Pull the record list out of a parsed export object.

    Accepts a container dict (``{"<key>": [...]}`` for one of ``container_keys``),
    a bare list, or a single record dict. Returns ``None`` if the shape is
    unrecognisable so the caller can loud-skip the whole file.
    """
    if isinstance(obj, dict):
        for key in container_keys:
            if isinstance(obj.get(key), list):
                return obj[key]
        # A single bare record object.
        return [obj]
    if isinstance(obj, list):
        return obj
    return None


def _iter_json_container(
    source: Source,
    *,
    container_keys: Iterable[str],
    file_globs: Iterable[str],
) -> Iterable[_RawRecord]:
    """Yield ``_RawRecord``s from a JSON export (file, dir, or in-memory object).

    A file that is neither valid JSON nor valid JSON-lines yields a single
    ``MALFORMED_JSON`` skip for that file (loud, not fatal). A directory is walked
    for the given globs; each file is parsed independently so one bad file never
    poisons the others.
    """
    # In-memory object (tests / already-parsed).
    if not isinstance(source, (str, os.PathLike)):
        yield from _records_from_obj(source, _source_name(source), container_keys)
        return

    path = os.fspath(source)
    files: List[str]
    if os.path.isdir(path):
        files = sorted(
            f for pattern in file_globs for f in glob.glob(os.path.join(path, pattern))
        )
    else:
        files = [path]

    for fpath in files:
        ref = os.path.basename(fpath)
        try:
            text = _read_text(fpath)
        except Exception:
            logger.warning("aws loader: could not read file %s", ref, exc_info=True)
            yield _RawRecord(ref, 0, error=UNREADABLE_FILE)
            continue
        yield from _records_from_text(text, ref, container_keys)


def _read_text(fpath: str) -> str:
    """Read a file as UTF-8 text, transparently gunzipping ``.gz`` (CloudTrail)."""
    if fpath.endswith(".gz"):
        with gzip.open(fpath, "rt", encoding="utf-8") as fh:
            return fh.read()
    with open(fpath, "r", encoding="utf-8") as fh:
        return fh.read()


def _records_from_text(
    text: str, ref: str, container_keys: Iterable[str]
) -> Iterable[_RawRecord]:
    """Parse one file's text into records: whole-JSON first, then JSON-lines."""
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        # Fall back to JSON-lines: parse each non-blank line independently so a
        # single bad line is one skip, not a whole-file failure.
        lines = [ln for ln in (text.splitlines()) if ln.strip()]
        if not lines:
            yield _RawRecord(ref, 0, error=MALFORMED_JSON)
            return
        produced = False
        for i, line in enumerate(lines):
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                produced = True
                yield _RawRecord(ref, i, error=MALFORMED_JSON)
                continue
            produced = True
            yield _RawRecord(ref, i, raw=rec if isinstance(rec, dict) else None,
                             error=None if isinstance(rec, dict) else NOT_AN_OBJECT)
        if not produced:
            yield _RawRecord(ref, 0, error=MALFORMED_JSON)
        return
    yield from _records_from_obj(obj, ref, container_keys, from_file=True)


def _records_from_obj(
    obj: Any, ref: str, container_keys: Iterable[str], *, from_file: bool = False
) -> Iterable[_RawRecord]:
    records = _coerce_records(obj, container_keys)
    if records is None:
        yield _RawRecord(ref, 0, error=MALFORMED_JSON if from_file else NOT_AN_OBJECT)
        return
    for i, rec in enumerate(records):
        if isinstance(rec, dict):
            yield _RawRecord(ref, i, raw=rec)
        else:
            yield _RawRecord(ref, i, error=NOT_AN_OBJECT)


# ---------------------------------------------------------------------------
# provider_event_id extractors (per format)
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
    return _bounded_event_id(f"{name}|{ts}|{item_type}")


def _eventbridge_event_id(raw: dict) -> Optional[str]:
    """EventBridge event envelope id (``id``)."""
    eid = raw.get("id")
    return _bounded_event_id(str(eid)) if eid else None


def _cloudtrail_event_id(raw: dict) -> Optional[str]:
    """CloudTrail record id (``eventID``)."""
    eid = raw.get("eventID")
    return _bounded_event_id(str(eid)) if eid else None


# ---------------------------------------------------------------------------
# Shared load driver
# ---------------------------------------------------------------------------


def _run_load(
    records: Iterable[_RawRecord],
    *,
    org_id: str,
    source_format: str,
    batch_id: str,
    source_reference: Optional[str],
    extract_event_id,
    sink: StagingSink,
) -> LoadResult:
    if not org_id or not str(org_id).strip():
        raise ValueError("org_id is required")

    result = LoadResult(
        org_id=org_id,
        provider=PROVIDER_AWS,
        source_format=source_format,
        batch_id=batch_id,
        source_reference=source_reference,
    )
    to_insert: List[OpsEventStagingRow] = []
    seen_ids: set[str] = set()

    for rec in records:
        if rec.error is not None:
            result.skipped.append(
                SkippedRecord(rec.error, _skip_detail(rec.error), rec.source_reference, rec.index)
            )
            logger.warning(
                "aws loader loud-skip [%s]: %s (source=%s index=%s)",
                rec.error, _skip_detail(rec.error), rec.source_reference, rec.index,
            )
            continue

        raw = rec.raw or {}
        event_id = extract_event_id(raw)
        if not event_id:
            result.skipped.append(
                SkippedRecord(MISSING_EVENT_ID, _skip_detail(MISSING_EVENT_ID),
                              rec.source_reference, rec.index)
            )
            logger.warning(
                "aws loader loud-skip [%s]: %s (source=%s index=%s)",
                MISSING_EVENT_ID, _skip_detail(MISSING_EVENT_ID),
                rec.source_reference, rec.index,
            )
            continue

        result.record_count += 1
        if event_id in seen_ids:
            # Within-batch duplicate — collapse to one row (idempotent load).
            result.duplicate_count += 1
            continue
        seen_ids.add(event_id)
        to_insert.append(
            OpsEventStagingRow(
                org_id=org_id,
                provider=PROVIDER_AWS,
                source_format=source_format,
                batch_id=batch_id,
                provider_event_id=event_id,
                raw=raw,
            )
        )

    inserted = sink.insert_rows(to_insert)
    result.inserted_count = inserted
    # Rows that were valid + unique in-batch but already present in the store.
    result.duplicate_count += len(to_insert) - inserted
    sink.record_batch(result.as_batch())

    logger.info(
        "aws loader done: source_format=%s batch=%s parsed=%d inserted=%d "
        "duplicates=%d skipped=%d",
        source_format, batch_id, result.record_count, result.inserted_count,
        result.duplicate_count, result.skipped_count,
    )
    return result


def _skip_detail(reason: str) -> str:
    return {
        MALFORMED_JSON: "record/file is not valid JSON",
        NOT_AN_OBJECT: "record is not a JSON object",
        MISSING_EVENT_ID: "no provider event identity to dedupe on",
        UNREADABLE_FILE: "file could not be read",
    }.get(reason, reason)


def _resolve_sink(sink: Optional[StagingSink]) -> StagingSink:
    return sink if sink is not None else DbStagingSink()


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
    history item type (no native id exists for alarm history).
    """
    bid = batch_id or _default_batch_id(SOURCE_FORMAT_CLOUDWATCH, source)
    records = _iter_json_container(
        source,
        container_keys=("AlarmHistoryItems",),
        file_globs=("*.json", "*.jsonl"),
    )
    return _run_load(
        records,
        org_id=org_id,
        source_format=SOURCE_FORMAT_CLOUDWATCH,
        batch_id=bid,
        source_reference=_source_name(source),
        extract_event_id=_cloudwatch_event_id,
        sink=_resolve_sink(sink),
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
    object. ``provider_event_id`` is the envelope ``id``.
    """
    bid = batch_id or _default_batch_id(SOURCE_FORMAT_EVENTBRIDGE, source)
    records = _iter_json_container(
        source,
        container_keys=("Events", "events"),
        file_globs=("*.json", "*.jsonl"),
    )
    return _run_load(
        records,
        org_id=org_id,
        source_format=SOURCE_FORMAT_EVENTBRIDGE,
        batch_id=bid,
        source_reference=_source_name(source),
        extract_event_id=_eventbridge_event_id,
        sink=_resolve_sink(sink),
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
    an in-memory object. ``provider_event_id`` is the record ``eventID``.
    """
    bid = batch_id or _default_batch_id(SOURCE_FORMAT_CLOUDTRAIL, source)
    records = _iter_json_container(
        source,
        container_keys=("Records",),
        file_globs=("*.json", "*.json.gz", "*.jsonl"),
    )
    return _run_load(
        records,
        org_id=org_id,
        source_format=SOURCE_FORMAT_CLOUDTRAIL,
        batch_id=bid,
        source_reference=_source_name(source),
        extract_event_id=_cloudtrail_event_id,
        sink=_resolve_sink(sink),
    )
