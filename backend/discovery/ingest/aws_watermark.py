"""MSP-B1 — per-scope time positions for the AWS poll source (resume correctness).

The AWS connector resumes each ``(account, region, surface)`` scope from an opaque
position string the skeleton stores in its checkpoint map. For the two time-series
surfaces (CloudWatch alarm history, CloudTrail management events) that position is
a **time watermark**: "every event at or before this instant has been ingested".

Two defects in the original watermark handling are fixed here.

1. Truncation lost events (backlog data loss)
---------------------------------------------
``LookupEvents`` returns events NEWEST-FIRST and there is no sort-order parameter.
The reader followed pages up to a safety cap and then advanced the watermark to the
newest event it had seen. With a backlog larger than the cap that is silent data
loss: the connector keeps the newest N events, moves the watermark past ALL of
them, and the older unread remainder can never be reached again — precisely the
"thins the data quietly" failure MSP-B1's failure-posture section forbids.

The fix is a **descending backfill window**. While a poll is truncated the
watermark does NOT move. Instead the position records:

* ``ceiling`` — the oldest instant already read, so the next poll reads the
  next-older chunk (the window walks backwards through the backlog);
* ``pending_high`` — the newest instant seen when the backfill started, held aside.

Only when the window finally drains does ``pending_high`` become the watermark and
the ceiling clear. The poll reports ``has_more`` so the skeleton keeps paging (the
skeleton bounds how many continuation polls one run performs, and the B7 budget
bounds the volume — both loudly).

The walk itself is also bounded in DEPTH (``max_backfill_seconds``), measured from
the backfill's own newest instant rather than wall-clock now. Without that bound an
initial load on a busy account walks the full 90-day ``LookupEvents`` retention with
the watermark pinned the whole way, so every NEW event queues behind the entire
history — the connector looks hung and the run never reaches the detectors. The
bound closes the window at a configured depth, promotes ``pending_high``, and says
so at WARNING; the omission of older events is a declared, configurable initial-load
boundary, not a silent thinning.

CloudWatch needs none of this: ``DescribeAlarmHistory`` accepts
``ScanBy='TimestampAscending'``, so reading oldest-first makes truncation safe by
construction — the watermark advances to the newest event actually read and the
unread remainder is simply newer.

2. Boundary events were dropped (same-instant stragglers)
----------------------------------------------------------
The filter was ``ts <= watermark`` on ISO **strings**. Two problems:

* String comparison is not time comparison. ``"2026-07-14T03:00:00Z"`` and
  ``"2026-07-14T03:00:00+00:00"`` are the same instant but compare unequal, and
  fractional seconds sort inconsistently against values without them. boto3 hands
  back ``datetime`` objects while fixtures carry strings, so both spellings really
  do occur. Comparison is now done on parsed datetimes.
* An event arriving later with a timestamp EQUAL to the stored watermark was
  dropped forever. That is not hypothetical: CloudTrail timestamps have
  second granularity and its delivery is eventually consistent, so a second event
  in an already-recorded second is routine. The position therefore also records
  the provider event ids seen AT the boundary instant, and an event at that exact
  instant is admitted when its id is not among them — no re-reads (the strict
  no-re-read rule of AC3 still holds) and no silent drops.

Wire format (deliberately backward-compatible)
----------------------------------------------
A position with nothing to remember beyond the instant encodes as the **plain ISO
string** it always was, so existing stored checkpoints keep working unchanged and
the common case stays readable. Only when boundary ids or an in-progress backfill
must be carried does it encode as compact JSON. :func:`decode_position` accepts
both, and :func:`watermark_of` reads the instant out of either.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

#: Wire-format version for the JSON encoding (a future shape change is detectable).
POSITION_VERSION = 1

#: Cap on how many boundary ids are carried at one instant. A pathological
#: same-second burst cannot bloat the checkpoint; past the cap the extra ids are
#: dropped, which can only cause a bounded re-delivery (B7 admission folds it),
#: never a silent loss.
MAX_BOUNDARY_IDS = 64


def parse_timestamp(value: Any) -> Optional[datetime]:
    """Parse a provider timestamp into an aware UTC datetime, or ``None``.

    Tolerates the three spellings that actually reach us: a boto3 ``datetime``, an
    ISO string with a ``Z`` suffix, and an ISO string with a numeric offset. A
    naive value is assumed UTC. Anything unparseable yields ``None`` so the caller
    can fall back rather than crash.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class TimePosition:
    """One time-series scope's resume position.

    ``watermark`` is the high-water instant: everything at or before it is
    ingested, except that events at exactly that instant whose id is NOT in
    ``boundary_ids`` are still admitted (same-instant stragglers).

    ``ceiling``/``ceiling_ids`` and ``pending_high``/``pending_high_ids`` are only
    populated while a descending backfill is walking a truncated backlog; they are
    cleared the moment the window drains.
    """

    watermark: str = ""
    boundary_ids: Tuple[str, ...] = ()
    ceiling: str = ""
    ceiling_ids: Tuple[str, ...] = ()
    pending_high: str = ""
    pending_high_ids: Tuple[str, ...] = ()

    @property
    def backfilling(self) -> bool:
        """True while a truncated descending backlog is still being walked."""
        return bool(self.ceiling)

    def is_new(self, timestamp: Any, event_id: str = "") -> bool:
        """True when an event at ``timestamp`` has not already been ingested.

        Strictly after the watermark, or exactly at it with an id not already
        recorded at that instant. An empty watermark admits everything (first run).
        """
        if not self.watermark:
            return True
        event_dt = parse_timestamp(timestamp)
        watermark_dt = parse_timestamp(self.watermark)
        if event_dt is None or watermark_dt is None:
            # Degenerate timestamp: fall back to the old string comparison rather
            # than admitting everything (which would re-read the world).
            return str(timestamp) > str(self.watermark)
        if event_dt > watermark_dt:
            return True
        if event_dt == watermark_dt:
            return bool(event_id) and event_id not in self.boundary_ids
        return False

    def below_ceiling(self, timestamp: Any, event_id: str = "") -> bool:
        """True when an event is inside the in-progress backfill window.

        No ceiling means no upper bound. At the ceiling instant the event is
        admitted only if it was not already read there — the same boundary rule as
        the watermark, applied to the other end of the window, so a same-second
        cluster split across a page boundary is neither dropped nor re-read.
        """
        if not self.ceiling:
            return True
        event_dt = parse_timestamp(timestamp)
        ceiling_dt = parse_timestamp(self.ceiling)
        if event_dt is None or ceiling_dt is None:
            return str(timestamp) < str(self.ceiling)
        if event_dt < ceiling_dt:
            return True
        if event_dt == ceiling_dt:
            return bool(event_id) and event_id not in self.ceiling_ids
        return False

    def accepts(self, timestamp: Any, event_id: str = "") -> bool:
        """True when an event falls inside this position's unread window."""
        return self.is_new(timestamp, event_id) and self.below_ceiling(timestamp, event_id)

    def to_wire(self) -> str:
        """Encode to the opaque position string (plain ISO when nothing else to carry)."""
        return encode_position(self)


def _ids_at(instant: str, events: Iterable[Tuple[str, str]]) -> Tuple[str, ...]:
    """The event ids whose timestamp equals ``instant`` (bounded, order-stable)."""
    target = parse_timestamp(instant)
    if target is None:
        return ()
    out: List[str] = []
    for timestamp, event_id in events:
        if not event_id:
            continue
        parsed = parse_timestamp(timestamp)
        if parsed is not None and parsed == target and event_id not in out:
            out.append(event_id)
            if len(out) >= MAX_BOUNDARY_IDS:
                break
    return tuple(out)


def encode_position(position: TimePosition) -> str:
    """Encode a :class:`TimePosition` as the scope's opaque position string.

    Emits the **plain ISO watermark** whenever there is nothing else to remember,
    which keeps stored checkpoints and their assertions in the original readable
    form; the compact JSON form appears only when boundary ids or a backfill
    window genuinely need carrying.
    """
    if not position.boundary_ids and not position.ceiling and not position.pending_high:
        return position.watermark
    payload: Dict[str, Any] = {"v": POSITION_VERSION, "wm": position.watermark}
    if position.boundary_ids:
        payload["wids"] = list(position.boundary_ids)
    if position.ceiling:
        payload["ceil"] = position.ceiling
        if position.ceiling_ids:
            payload["cids"] = list(position.ceiling_ids)
    if position.pending_high:
        payload["hi"] = position.pending_high
        if position.pending_high_ids:
            payload["hids"] = list(position.pending_high_ids)
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def decode_position(value: Optional[str]) -> TimePosition:
    """Decode an opaque position string — plain ISO or JSON — tolerantly.

    An empty value is a first run. An unparseable value degrades to a first run
    (a full, safe re-read) rather than raising, matching the skeleton's own
    checkpoint-decode posture.
    """
    if not value:
        return TimePosition()
    text = str(value).strip()
    if not text.startswith("{"):
        return TimePosition(watermark=text)
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        logger.warning(
            "aws_watermark: could not decode a scope position; treating the scope "
            "as a first run (full re-poll rather than a silent skip)."
        )
        return TimePosition()
    if not isinstance(data, dict):
        return TimePosition()

    def _tuple(key: str) -> Tuple[str, ...]:
        raw = data.get(key)
        return tuple(str(x) for x in raw) if isinstance(raw, list) else ()

    return TimePosition(
        watermark=str(data.get("wm") or ""),
        boundary_ids=_tuple("wids"),
        ceiling=str(data.get("ceil") or ""),
        ceiling_ids=_tuple("cids"),
        pending_high=str(data.get("hi") or ""),
        pending_high_ids=_tuple("hids"),
    )


def watermark_of(value: Optional[str]) -> str:
    """The high-water instant of a position string, whichever form it is in.

    The accessor callers (and contract tests) should use instead of comparing the
    raw position string, so the wire format can carry boundary/backfill state
    without every reader needing to know about it.
    """
    return decode_position(value).watermark


def advance_ascending(
    previous: TimePosition, events: List[Tuple[str, str]], *, truncated: bool
) -> TimePosition:
    """Next position after an OLDEST-FIRST read (CloudWatch alarm history).

    Ascending order makes truncation safe: everything up to the newest event read
    is complete, so the watermark advances to it and the unread remainder is newer
    and simply polled next. ``truncated`` therefore does not change the result — it
    only tells the caller to keep paging.
    """
    newest = _max_timestamp([timestamp for timestamp, _ in events], previous.watermark)
    if not newest:
        return previous
    if newest == previous.watermark:
        # Same instant: merge any newly-seen ids so a straggler is not re-read.
        merged = tuple(
            dict.fromkeys(list(previous.boundary_ids) + list(_ids_at(newest, events)))
        )[:MAX_BOUNDARY_IDS]
        return TimePosition(watermark=newest, boundary_ids=merged)
    return TimePosition(watermark=newest, boundary_ids=_ids_at(newest, events))


def advance_descending(
    previous: TimePosition,
    events: List[Tuple[str, str]],
    *,
    truncated: bool,
    max_backfill_seconds: Optional[float] = None,
) -> TimePosition:
    """Next position after a NEWEST-FIRST read (CloudTrail ``LookupEvents``).

    While ``truncated`` the watermark is pinned and the window's ceiling walks down
    to the oldest instant just read, so the next poll continues into the older
    backlog instead of skipping it. When the read completes, the newest instant
    seen across the whole backfill (held in ``pending_high``) becomes the watermark
    and the window clears.

    ``max_backfill_seconds`` bounds how far back an initial load walks, measured
    from the backfill's own newest instant (``pending_high``) — NOT from wall-clock
    now, so it is independent of when the run happens. ``LookupEvents`` retains 90
    days; on a busy account walking all of it holds the watermark pinned for many
    runs, which delays every NEW event behind the whole historical backlog. Once the
    window has walked past this depth the backfill is closed: ``pending_high`` is
    promoted to the watermark and steady-state incremental polling resumes. Events
    older than the depth are consequently NOT ingested — that is a bounded initial
    load, logged at WARNING and configurable, never a silent thinning. ``None``/``0``
    keeps the unbounded walk.
    """
    timestamps = [timestamp for timestamp, _ in events]
    page_high = _max_timestamp(timestamps, "")
    page_low = _min_timestamp(timestamps)

    # The newest instant of the whole backfill is fixed by its FIRST page, since
    # the read walks strictly backwards from there.
    pending_high = previous.pending_high or page_high
    pending_high_ids = (
        previous.pending_high_ids
        if previous.pending_high
        else _ids_at(page_high, events)
    )

    if (
        truncated
        and page_low
        and max_backfill_seconds
        and _exceeds_backfill_depth(pending_high, page_low, max_backfill_seconds)
    ):
        logger.warning(
            "aws_watermark: initial backfill reached its depth bound (%.0fs before "
            "%s) at %s — closing the window and resuming incremental polling. "
            "Events older than that are NOT ingested; raise "
            "AWS_EVENT_MAX_BACKFILL_DAYS to widen the initial load.",
            max_backfill_seconds, pending_high, page_low,
        )
        truncated = False  # fall through to the drain path: promote pending_high

    if truncated and page_low:
        new_ceiling = page_low
        if previous.ceiling and parse_timestamp(new_ceiling) is not None:
            previous_ceiling = parse_timestamp(previous.ceiling)
            if previous_ceiling is not None and parse_timestamp(new_ceiling) >= previous_ceiling:
                # No backwards progress — stop the walk rather than spin, and keep
                # the watermark pinned so nothing is skipped. The next run retries.
                logger.warning(
                    "aws_watermark: descending backfill made no progress at %s — "
                    "pausing the walk; the backlog is retried next run, not skipped",
                    previous.ceiling,
                )
                return previous
        return TimePosition(
            watermark=previous.watermark,
            boundary_ids=previous.boundary_ids,
            ceiling=new_ceiling,
            ceiling_ids=_ids_at(new_ceiling, events),
            pending_high=pending_high,
            pending_high_ids=pending_high_ids,
        )

    # Window drained: promote the backfill's high-water mark.
    final = _max_timestamp([pending_high], previous.watermark)
    if not final:
        return TimePosition(watermark=previous.watermark, boundary_ids=previous.boundary_ids)
    if final == previous.watermark:
        merged = tuple(
            dict.fromkeys(list(previous.boundary_ids) + list(pending_high_ids))
        )[:MAX_BOUNDARY_IDS]
        return TimePosition(watermark=final, boundary_ids=merged)
    ids = pending_high_ids if final == pending_high else _ids_at(final, events)
    return TimePosition(watermark=final, boundary_ids=ids)


def _exceeds_backfill_depth(high: str, low: str, max_seconds: float) -> bool:
    """True when a descending walk has gone ``max_seconds`` below its own high mark.

    Tolerant: if either instant is unparseable the depth cannot be established and
    the walk continues (the bound must never end a backfill on a bad timestamp).
    """
    high_dt, low_dt = parse_timestamp(high), parse_timestamp(low)
    if high_dt is None or low_dt is None:
        return False
    return (high_dt - low_dt).total_seconds() >= max_seconds


def _max_timestamp(timestamps: Iterable[Any], fallback: str) -> str:
    """The latest of ``timestamps`` (compared as instants), else ``fallback``."""
    best_text = fallback
    best_dt = parse_timestamp(fallback)
    for timestamp in timestamps:
        if timestamp in (None, ""):
            continue
        parsed = parse_timestamp(timestamp)
        if parsed is None:
            continue
        if best_dt is None or parsed > best_dt:
            best_dt, best_text = parsed, str(timestamp)
    return best_text


def _min_timestamp(timestamps: Iterable[Any]) -> str:
    """The earliest of ``timestamps`` (compared as instants), or ``""``."""
    best_text = ""
    best_dt: Optional[datetime] = None
    for timestamp in timestamps:
        if timestamp in (None, ""):
            continue
        parsed = parse_timestamp(timestamp)
        if parsed is None:
            continue
        if best_dt is None or parsed < best_dt:
            best_dt, best_text = parsed, str(timestamp)
    return best_text
