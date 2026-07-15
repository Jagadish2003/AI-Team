"""Shared machinery for the MSP-B8 export loaders — AWS (T2) and Azure (T3).

Both provider loader families parse "standard export JSON" into the SAME staging
schema with the SAME loud-skip / idempotency / batch-registry discipline. That
shared behaviour lives here ONCE — the AWS and Azure modules supply only the
per-provider edges (which container key holds the records, how to pull the
``provider_event_id``, and how to pull the event timestamp). This is the same
anti-drift stance R17-A4 took by sharing ``operational_signals.py`` between Java
and .NET: the equivalence the story demands (AWS and Azure land indistinguishable
except by ``provider``/``source_system``) is guaranteed by construction, not by
two parallel implementations agreeing by accident.

Nothing here maps to the detector-facing model — that is the B0 mapper's job,
invoked later by the bridge (T4). These loaders normalise ONLY the staging
metadata (``provider_event_id`` for dedupe, ``event_time`` for ordering) and keep
the raw payload intact.
"""
from __future__ import annotations

import glob
import gzip
import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, List, Optional, Union

from database.models.ops_event_staging import (
    OpsEventLoadBatch,
    OpsEventStagingRow,
    _PROVIDER_EVENT_ID_LEN,
)
from discovery.ingest.ops_event_staging_store import DbStagingSink, StagingSink

logger = logging.getLogger(__name__)

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

#: Extracts the provider event identity from a raw record (None => loud-skip).
EventIdExtractor = Callable[[dict], Optional[str]]
#: Extracts the provider event timestamp from a raw record (None => leave NULL).
EventTimeExtractor = Callable[[dict], Optional[datetime]]


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
# Value helpers
# ---------------------------------------------------------------------------


def bounded_event_id(value: str) -> str:
    """Return an id that fits ``provider_event_id``; hash if it would overflow.

    A composite / very long id can exceed the column width; hashing keeps it
    deterministic — the same input always yields the same id, so idempotency
    holds — while staying in bounds.
    """
    if len(value) <= _PROVIDER_EVENT_ID_LEN:
        return value
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


_FRACTION_RE = re.compile(r"(\.\d{6})\d+")


def parse_timestamp(value: Any) -> Optional[datetime]:
    """Best-effort parse of a provider timestamp string to an aware datetime.

    Tolerant on purpose — ``event_time`` is "where available", never load-bearing:
    a value that will not parse yields ``None`` (the column stays NULL) rather than
    failing the record. Handles ISO-8601 with ``Z`` and Azure's 7-digit (100-ns)
    fractional seconds, which ``datetime.fromisoformat`` rejects, by truncating the
    fraction to microseconds. A naive result is assumed UTC.
    """
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    s = _FRACTION_RE.sub(r"\1", s)  # >6 fractional digits -> exactly 6
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def source_name(source: Source) -> str:
    if isinstance(source, (str, os.PathLike)):
        return os.path.basename(os.path.normpath(os.fspath(source))) or os.fspath(source)
    return "<memory>"


def default_batch_id(provider: str, source_format: str, source: Source) -> str:
    """Deterministic default batch id (T1 doc §5.1 convention).

    Stable across re-loads of the same source (no timestamp), so re-running a load
    reuses the batch id — aids auditing — while idempotency is guaranteed by
    ``provider_event_id`` regardless of the batch id.
    """
    return f"{provider}:{source_format}:{source_name(source)}"


def resolve_sink(sink: Optional[StagingSink]) -> StagingSink:
    return sink if sink is not None else DbStagingSink()


def skip_detail(reason: str) -> str:
    return {
        MALFORMED_JSON: "record/file is not valid JSON",
        NOT_AN_OBJECT: "record is not a JSON object",
        MISSING_EVENT_ID: "no provider event identity to dedupe on",
        UNREADABLE_FILE: "file could not be read",
    }.get(reason, reason)


def dig(raw: dict, *paths: Iterable[str]) -> Any:
    """Return the first present value among dotted-ish key paths.

    ``dig(raw, ("id",), ("data", "essentials", "alertId"))`` returns
    ``raw["id"]`` if set, else ``raw["data"]["essentials"]["alertId"]``. Each path
    is a sequence of keys walked through nested dicts; a missing/!dict step skips
    that path. Keeps the Azure extractors (which face two envelope shapes) tidy.
    """
    for path in paths:
        cur: Any = raw
        ok = True
        for key in path:
            if isinstance(cur, dict) and key in cur:
                cur = cur[key]
            else:
                ok = False
                break
        if ok and cur not in (None, ""):
            return cur
    return None


# ---------------------------------------------------------------------------
# Format readers (JSON container / JSON-lines / dir / gzip / in-memory)
# ---------------------------------------------------------------------------


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
        return [obj]  # a single bare record object
    if isinstance(obj, list):
        return obj
    return None


def _read_text(fpath: str) -> str:
    """Read a file as UTF-8 text, transparently gunzipping ``.gz`` (CloudTrail)."""
    if fpath.endswith(".gz"):
        with gzip.open(fpath, "rt", encoding="utf-8") as fh:
            return fh.read()
    with open(fpath, "r", encoding="utf-8") as fh:
        return fh.read()


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


def _records_from_text(
    text: str, ref: str, container_keys: Iterable[str]
) -> Iterable[_RawRecord]:
    """Parse one file's text into records: whole-JSON first, then JSON-lines."""
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        lines = [ln for ln in text.splitlines() if ln.strip()]
        if not lines:
            yield _RawRecord(ref, 0, error=MALFORMED_JSON)
            return
        for i, line in enumerate(lines):
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                yield _RawRecord(ref, i, error=MALFORMED_JSON)
                continue
            yield _RawRecord(
                ref, i,
                raw=rec if isinstance(rec, dict) else None,
                error=None if isinstance(rec, dict) else NOT_AN_OBJECT,
            )
        return
    yield from _records_from_obj(obj, ref, container_keys, from_file=True)


def iter_json_container(
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
    if not isinstance(source, (str, os.PathLike)):
        yield from _records_from_obj(source, source_name(source), container_keys)
        return

    path = os.fspath(source)
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
            logger.warning("ops-event loader: could not read file %s", ref, exc_info=True)
            yield _RawRecord(ref, 0, error=UNREADABLE_FILE)
            continue
        yield from _records_from_text(text, ref, container_keys)


# ---------------------------------------------------------------------------
# Shared load driver
# ---------------------------------------------------------------------------


def run_load(
    records: Iterable[_RawRecord],
    *,
    org_id: str,
    provider: str,
    source_format: str,
    batch_id: str,
    source_reference: Optional[str],
    extract_event_id: EventIdExtractor,
    extract_event_time: EventTimeExtractor,
    sink: StagingSink,
) -> LoadResult:
    if not org_id or not str(org_id).strip():
        raise ValueError("org_id is required")

    result = LoadResult(
        org_id=org_id,
        provider=provider,
        source_format=source_format,
        batch_id=batch_id,
        source_reference=source_reference,
    )
    to_insert: List[OpsEventStagingRow] = []
    seen_ids: set[str] = set()

    def _skip(reason: str, rec: _RawRecord) -> None:
        result.skipped.append(
            SkippedRecord(reason, skip_detail(reason), rec.source_reference, rec.index)
        )
        logger.warning(
            "ops-event loader loud-skip [%s]: %s (provider=%s source=%s index=%s)",
            reason, skip_detail(reason), provider, rec.source_reference, rec.index,
        )

    for rec in records:
        if rec.error is not None:
            _skip(rec.error, rec)
            continue

        raw = rec.raw or {}
        event_id = extract_event_id(raw)
        if not event_id:
            _skip(MISSING_EVENT_ID, rec)
            continue

        result.record_count += 1
        if event_id in seen_ids:
            result.duplicate_count += 1  # within-batch duplicate collapses
            continue
        seen_ids.add(event_id)
        to_insert.append(
            OpsEventStagingRow(
                org_id=org_id,
                provider=provider,
                source_format=source_format,
                batch_id=batch_id,
                provider_event_id=event_id,
                raw=raw,
                event_time=extract_event_time(raw),
            )
        )

    inserted = sink.insert_rows(to_insert)
    result.inserted_count = inserted
    result.duplicate_count += len(to_insert) - inserted  # already present in store
    sink.record_batch(result.as_batch())

    logger.info(
        "ops-event loader done: provider=%s source_format=%s batch=%s parsed=%d "
        "inserted=%d duplicates=%d skipped=%d",
        provider, source_format, batch_id, result.record_count, result.inserted_count,
        result.duplicate_count, result.skipped_count,
    )
    return result
