"""MSP-B1 / AT-642 (T2) — the live AWS poll source.

The provider edge for the native AWS event connector: a
:class:`~discovery.ingest.cloud_event_connector.CloudPollSource` that turns the
managed-account config into scopes and, per scope, authenticates via
:class:`~discovery.ingest.aws_auth.AWSAuthenticator` (STS AssumeRole from the hub
identity, or direct per-account keys) and reads that surface's operational events
with EXACTLY the read-only API calls the partner IAM policy grants:

* **CloudWatch** — ``cloudwatch:DescribeAlarmHistory`` (alarm state changes),
  transformed into the ``CloudWatch Alarm State Change`` event shape the MSP-B0
  ``map_cloudwatch`` reference mapper consumes (the T1 skeleton then normalises it).
* **EventBridge** — ``events:ListRules`` + ``events:DescribeRule`` on the scoped
  rules (the *bounded* EventBridge surface reachable through the granted
  ``events:Describe*/List*`` — the event bus itself exposes no past-event read API;
  full event-stream ingestion via an archive replay is a documented follow-up).
* **CloudTrail** — ``cloudtrail:LookupEvents`` (management events).

Every scope carries its ``account_scope`` (the managed account id), which the
skeleton stamps onto every emitted record — so a multi-account run never loses
which account a signal belongs to (AT-642 AC1).

Across-run resume is by a per-scope **time watermark** (the newest event time seen
for that scope), carried as the skeleton's opaque per-scope checkpoint position.
Within a run each reader follows the provider's ``NextToken`` internally (bounded
by :data:`MAX_PAGES_PER_POLL`) and returns everything newer than the watermark in
one page; how many further continuation polls a run performs is bounded by the
shared skeleton (per-scope poll cap, wall-clock deadline, and the B7 admission
budget), and the DEPTH of an initial CloudTrail backfill is bounded by
:data:`DEFAULT_MAX_BACKFILL_DAYS`. Every one of those bounds reports what it stopped
— a partial ingest is never allowed to read as a complete one.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from .aws_auth import AWSAccountConfig, AWSAuthenticator, AWSCredentials
from .aws_event_connector import (
    AWS_SURFACE_MAPPERS,
    AWS_SURFACES,
    PROVIDER_AWS,
    SURFACE_CLOUDTRAIL,
    SURFACE_CLOUDWATCH,
    SURFACE_EVENTBRIDGE,
    aws_scope,
)
from .aws_health import AWSConnectorHealth, is_throttle_error
from .aws_partitions import arn_partition_for_region
from .aws_watermark import (
    TimePosition,
    advance_ascending,
    advance_descending,
    decode_position,
    encode_position,
    parse_timestamp,
    watermark_of,
)
from .cloud_event_connector import CloudPollSource, CloudScope, PollPage

logger = logging.getLogger(__name__)

#: Safety cap on provider pages followed within a single poll() call, so a huge
#: backlog cannot spin unbounded. Hitting it is logged loudly (never silent).
MAX_PAGES_PER_POLL = 50

#: Default provider page size per surface API call.
DEFAULT_PAGE_SIZE = 100

#: ``cloudtrail:LookupEvents`` caps ``MaxResults`` at 50 — asking for more is a
#: request the API is not obliged to honour, so the reader clamps to the documented
#: maximum rather than relying on provider-side leniency.
CLOUDTRAIL_MAX_RESULTS = 50

#: Default max throttle back-off retries before a scope is reported failed (loud).
DEFAULT_MAX_THROTTLE_RETRIES = 5

#: Default DEPTH of an initial CloudTrail backfill, in days, measured from the
#: backfill's own newest event (see :mod:`discovery.ingest.aws_watermark`). 30 days
#: matches the month-scale window MSP-B7's volume calibration was derived from
#: (``docs/MSP-B8_VOLUME_VALIDATION.md``) and keeps a first load convergent instead
#: of walking the full 90-day LookupEvents retention with the watermark pinned.
#: ``0`` restores the unbounded walk.
DEFAULT_MAX_BACKFILL_DAYS = 30

#: Back-off schedule (seconds) — exponential, capped. Tests inject a no-op sleeper.
_BACKOFF_BASE_SECONDS = 0.5
_BACKOFF_CAP_SECONDS = 10.0


def _configured_max_backfill_days() -> float:
    """Initial-backfill depth in days from ``AWS_EVENT_MAX_BACKFILL_DAYS``.

    The env name is spelled literally (not via a constant) so the ingest-layer env
    guard can statically confirm it is a numeric tuning knob and not a credential.
    """
    raw = os.environ.get("AWS_EVENT_MAX_BACKFILL_DAYS")
    if raw is None or not str(raw).strip():
        return DEFAULT_MAX_BACKFILL_DAYS
    try:
        value = float(str(raw).strip())
    except ValueError:
        logger.warning(
            "aws_poll_source: AWS_EVENT_MAX_BACKFILL_DAYS=%r is not a number — "
            "using the default %s day(s)", raw, DEFAULT_MAX_BACKFILL_DAYS,
        )
        return DEFAULT_MAX_BACKFILL_DAYS
    return max(0.0, value)


#: Surface → the boto3 service name whose client reads it.
_SERVICE_FOR_SURFACE: Dict[str, str] = {
    SURFACE_CLOUDWATCH: "cloudwatch",
    SURFACE_EVENTBRIDGE: "events",
    SURFACE_CLOUDTRAIL: "cloudtrail",
}

#: What the EventBridge surface can and cannot observe — stated plainly so the
#: boundary is documented rather than implied (MSP-B1 SCOPE DEFENCE).
#:
#: MSP-B1 lists EventBridge as one of the three V1 event classes and describes it
#: as "archive/replay-adjacent reads on the bounded rule set" (Section 1). The
#: minimal read-only grant the partner IAM policy asks for is
#: ``events:Describe*/List*``, which reads RULE CONFIGURATION. The EventBridge bus
#: itself exposes no read API for past events, so what this surface honestly
#: contributes is the bounded rule set as observed operational state plus every
#: subsequent rule CHANGE (a rule appearing, being modified, or being disabled IS
#: an operational event, and is what the checkpoint's rule-signature map detects).
#:
#: LIVE DATA / EXTRA GRANT NEEDED to go further: a true EventBridge *event stream*
#: requires the customer to route the bounded rules to a durable target the
#: connector can then read (a CloudWatch Logs log group, S3, or Firehose), or to
#: create an EventBridge Archive and replay it. Both are customer-side estate
#: changes beyond the read-only policy in ``deployment/aws_readonly_iam_policy.json``
#: and are deliberately out of V1 scope — widening is a new story, per the story's
#: own scope-defence note.
EVENTBRIDGE_SURFACE_NOTE = (
    "EventBridge V1 reads the bounded RULE SET via events:ListRules/DescribeRule "
    "(the bus exposes no past-event read API under the minimal read-only grant). "
    "The rule set is emitted once as observed state, then only on change. A true "
    "event stream needs a durable rule target (CloudWatch Logs / S3 / Firehose) or "
    "an EventBridge Archive replay — a customer-side estate change, out of V1 scope."
)


# ─────────────────────────────────────────────────────────────────────────────
# Surface readers (pure over a client — tests inject a fake client)
# ─────────────────────────────────────────────────────────────────────────────

def _bounded_id(value: str, *, limit: int = 256) -> str:
    """Bound an identity string so a pathological value cannot bloat a key."""
    return value if len(value) <= limit else value[:limit]


def _iso(value: Any) -> str:
    """Normalise a provider timestamp to a canonical ISO-8601 string.

    boto3/botocore deserialise API timestamps to aware ``datetime`` objects, while
    offline fixtures carry ISO strings. Both must produce the SAME string so the
    per-account watermark (checkpoint) compares consistently across a fixture run
    and a live run — a datetime is rendered via ``isoformat()``, a string passes
    through, anything else degrades to ``str()``.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return value.isoformat()
    except AttributeError:
        return str(value)


def _watermark_to_datetime(watermark: str) -> Optional[datetime]:
    """Parse a stored position's watermark to an aware datetime (back-compat shim).

    Superseded by :func:`discovery.ingest.aws_watermark.parse_timestamp`, which the
    readers now use directly. Retained because it accepts a whole *position* string
    (plain ISO or the richer JSON form) rather than a bare timestamp, and callers
    outside the readers may still hold one.
    """
    return parse_timestamp(watermark_of(watermark))


def _alarm_history_to_state_change(
    item: Dict[str, Any], *, account_id: str, region: Optional[str],
    timestamp_iso: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Adapt one ``DescribeAlarmHistory`` item to the Alarm State Change shape.

    The MSP-B0 ``map_cloudwatch`` reference mapper consumes the EventBridge-
    delivered *Alarm State Change* event (``detail.state.value`` / ``resources`` /
    ``detail-type``), not the ``DescribeAlarmHistory`` item shape. Reconciling the
    two here (the connector's provider edge) is exactly the gap the MSP-B8 T6
    equivalence harness flagged for B1 to resolve — so the native connector emits
    detector-identical events without changing the B0 mapper contract.

    ``timestamp_iso`` is the caller-normalised item time (see :func:`_iso`); when
    omitted it is derived from the item. Returns ``None`` (loud-skip) for an item
    with no alarm name / timestamp — it has no stable identity and cannot be a
    state-change event.
    """
    name = item.get("AlarmName")
    ts = timestamp_iso if timestamp_iso is not None else _iso(item.get("Timestamp"))
    if not name or not ts:
        return None
    item_type = item.get("HistoryItemType", "")
    new_state, old_state = _parse_alarm_history_states(item.get("HistoryData"))
    # Partition-aware ARN (AT-645): GovCloud resources are arn:aws-us-gov:…
    arn_partition = arn_partition_for_region(region)
    alarm_arn = f"arn:{arn_partition}:cloudwatch:{region or ''}:{account_id}:alarm:{name}"
    return {
        "id": _bounded_id(f"{name}|{ts}|{item_type}"),
        "detail-type": "CloudWatch Alarm State Change",
        "source": "aws.cloudwatch",
        "account": account_id,
        "time": ts,
        "region": region,
        "resources": [alarm_arn],
        "detail": {
            "alarmName": name,
            "state": {"value": new_state, "reason": item.get("HistorySummary")},
            "previousState": {"value": old_state},
        },
    }


def _parse_alarm_history_states(history_data: Any) -> Tuple[str, str]:
    """Extract ``(newStateValue, oldStateValue)`` from a HistoryData blob (tolerant)."""
    data = history_data
    if isinstance(history_data, str):
        try:
            data = json.loads(history_data)
        except (TypeError, ValueError):
            data = {}
    if not isinstance(data, dict):
        return "", ""
    new_state = (data.get("newState") or {}).get("stateValue", "") if isinstance(data.get("newState"), dict) else ""
    old_state = (data.get("oldState") or {}).get("stateValue", "") if isinstance(data.get("oldState"), dict) else ""
    return new_state, old_state


def read_cloudwatch(
    client: Any, *, region: Optional[str], account_id: str, watermark: str, page_size: int
) -> Tuple[List[Dict[str, Any]], str, bool]:
    """Read new CloudWatch alarm-history state changes since ``watermark`` (AT-643).

    V1 scope: CloudWatch **alarm state changes only** — ``DescribeAlarmHistory``
    filtered to ``HistoryItemType='StateUpdate'`` — NOT CloudWatch metrics or
    CloudWatch Logs. ``StartDate`` narrows the server-side window to the account's
    checkpoint, and the client-side position filter is the authoritative
    incremental guard so a second run re-reads nothing (AC3).

    Read **oldest-first** (``ScanBy='TimestampAscending'``). That ordering is what
    makes hitting :data:`MAX_PAGES_PER_POLL` safe: the events read are a complete
    prefix of the backlog, so the watermark can advance to the newest one read and
    the unread remainder — which is strictly newer — is simply picked up by the
    next poll. Reading newest-first and then advancing the watermark past
    everything (the original behaviour) discarded the older remainder for good.

    Returns ``(events, next_position, truncated)``; ``truncated`` tells the caller
    a backlog remains so it can keep paging in the same run.
    """
    position = decode_position(watermark)
    events: List[Dict[str, Any]] = []
    seen: List[Tuple[str, str]] = []            # (timestamp, provider event id)
    start_date = parse_timestamp(position.watermark)
    token: Optional[str] = None
    truncated = False
    for page_number in range(MAX_PAGES_PER_POLL):
        params: Dict[str, Any] = {
            "HistoryItemType": "StateUpdate",
            "MaxRecords": page_size,
            # Oldest-first: see the docstring — this is the anti-truncation-loss
            # property, not a cosmetic choice.
            "ScanBy": "TimestampAscending",
        }
        if start_date is not None:
            params["StartDate"] = start_date
        if token:
            params["NextToken"] = token
        resp = client.describe_alarm_history(**params) or {}
        for item in resp.get("AlarmHistoryItems", []) or []:
            # V1 scope defence (belt-and-suspenders beyond the StateUpdate server
            # filter): only alarm STATE changes — never a metric/config history
            # item or CloudWatch Logs (AT-644 AC2).
            if item.get("HistoryItemType") != "StateUpdate":
                continue
            ts = _iso(item.get("Timestamp"))
            event = _alarm_history_to_state_change(
                item, account_id=account_id, region=region, timestamp_iso=ts
            )
            if event is None:
                continue
            # Authoritative incremental guard, on parsed instants and aware of the
            # ids already recorded at the boundary instant (AC3: no re-reads; and
            # no same-instant straggler dropped either).
            if not position.accepts(ts, str(event.get("id") or "")):
                continue
            events.append(event)
            seen.append((ts, str(event.get("id") or "")))
        token = resp.get("NextToken")
        if not token:
            break
        if page_number == MAX_PAGES_PER_POLL - 1:
            truncated = True
            logger.warning(
                "aws_poll_source: cloudwatch hit MAX_PAGES_PER_POLL for account %s — "
                "resuming from the advanced watermark (no events dropped)",
                account_id,
            )
    advanced = advance_ascending(position, seen, truncated=truncated)
    next_position = encode_position(advanced)
    # Only report "keep paging" when the position actually moved, so a source that
    # reports a backlog it cannot advance past can never spin.
    return events, next_position, truncated and next_position != watermark


def read_cloudtrail(
    client: Any,
    *,
    region: Optional[str],
    account_id: str,
    watermark: str,
    page_size: int,
    max_backfill_seconds: Optional[float] = None,
) -> Tuple[List[Dict[str, Any]], str, bool]:
    """Read new CloudTrail **management** events since ``watermark`` (AT-644).

    V1 scope: management (audit) events only — ``cloudtrail:LookupEvents`` returns
    management events by design (data events are never returned), and
    :func:`_is_management_event` drops any explicit data/Insight record that slips
    through (AC2). ``StartTime`` narrows the server window to the account's
    checkpoint; the client-side position filter is the authoritative no-re-read
    incremental guard (AC3).

    ``LookupEvents`` is **newest-first** and offers no sort-order parameter, so a
    backlog larger than :data:`MAX_PAGES_PER_POLL` cannot simply advance the
    watermark — that would strand every older unread event permanently. Instead the
    poll walks the backlog **backwards**: while truncated the watermark stays
    pinned and the position's ceiling drops to the oldest instant just read, so the
    next poll continues into the older remainder. The newest instant is held in the
    position and promoted to the watermark only once the window drains. See
    :mod:`discovery.ingest.aws_watermark`.

    ``max_backfill_seconds`` bounds how DEEP an initial backfill walks (measured from
    the backfill's own newest event), so a first load on an account with months of
    audit history converges instead of pinning the watermark across many runs. It is
    passed through to :func:`advance_descending`, which closes the window loudly.

    Returns ``(events, next_position, truncated)``.
    """
    position = decode_position(watermark)
    events: List[Dict[str, Any]] = []
    seen: List[Tuple[str, str]] = []            # (timestamp, provider event id)
    start_time = parse_timestamp(position.watermark)
    # An in-progress backfill bounds the server window from above too, so each
    # continuation asks for the next-older slice rather than re-fetching the newest.
    end_time = parse_timestamp(position.ceiling) if position.backfilling else None
    token: Optional[str] = None
    truncated = False
    max_results = min(page_size, CLOUDTRAIL_MAX_RESULTS)
    for page_number in range(MAX_PAGES_PER_POLL):
        params: Dict[str, Any] = {"MaxResults": max_results}
        if start_time is not None:
            params["StartTime"] = start_time
        if end_time is not None:
            params["EndTime"] = end_time
        if token:
            params["NextToken"] = token
        resp = client.lookup_events(**params) or {}
        for ev in resp.get("Events", []) or []:
            record = _parse_cloudtrail_event(ev)
            if record is None:
                continue
            if not _is_management_event(record):
                continue  # V1 scope: never data / Insight events (AC2)
            ts = _iso(record.get("eventTime"))
            event_id = str(record.get("eventID") or "")
            if not position.accepts(ts, event_id):
                continue
            events.append(record)
            seen.append((ts, event_id))
        token = resp.get("NextToken")
        if not token:
            break
        if page_number == MAX_PAGES_PER_POLL - 1:
            truncated = True
            logger.warning(
                "aws_poll_source: cloudtrail hit MAX_PAGES_PER_POLL for account %s — "
                "walking the remaining backlog backwards (no events dropped)",
                account_id,
            )
    advanced = advance_descending(
        position, seen, truncated=truncated, max_backfill_seconds=max_backfill_seconds
    )
    next_position = encode_position(advanced)
    # Only report "keep paging" when the position actually moved AND the window is
    # still open — a backfill closed by the depth bound has nothing left to page.
    return (
        events,
        next_position,
        truncated and advanced.backfilling and next_position != watermark,
    )


def _is_management_event(record: Dict[str, Any]) -> bool:
    """True when a CloudTrail record is a management (audit) event — V1 scope.

    Defensive: an explicit ``eventCategory`` of ``Data``/``Insight`` or
    ``managementEvent == False`` is out of scope and dropped. Records that carry
    neither marker (older CloudTrail shapes) are treated as management, matching
    what ``LookupEvents`` returns.
    """
    category = str(record.get("eventCategory") or "").strip().lower()
    if category in ("data", "insight"):
        return False
    if record.get("managementEvent") is False:
        return False
    return True


def _parse_cloudtrail_event(ev: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Unwrap a LookupEvents entry to the raw CloudTrail record (map_cloudtrail shape)."""
    raw = ev.get("CloudTrailEvent")
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            logger.warning("aws_poll_source: unparseable CloudTrailEvent — skipped")
            return None
    if isinstance(raw, dict):
        return raw
    # Some fixtures/paths hand the record fields directly on the entry.
    return ev if ev.get("eventID") or ev.get("eventName") else None


def read_eventbridge(
    client: Any, *, region: Optional[str], account_id: str, watermark: str, page_size: int
) -> Tuple[List[Dict[str, Any]], str, bool]:
    """Read the bounded EventBridge rule set as operational events (AT-644).

    Uses ``events:ListRules`` + ``events:DescribeRule`` on the scoped rules — the
    bounded surface reachable through the granted ``events:Describe*/List*``. See
    :data:`EVENTBRIDGE_SURFACE_NOTE` for the exact boundary of what this surface
    can observe and what a fuller event stream would require; the short version is
    that the bus exposes no past-event read API, so the bounded rule set (and every
    subsequent change to it) IS the observable operational signal here.

    Incremental (AC3): the rule set is configuration, not a time series, so the
    per-scope checkpoint is a compact ``{rule_key: signature}`` map instead of a
    timestamp. A rule is emitted only when it is NEW or its signature CHANGED; an
    unchanged rule set on a second run yields no events (no re-reads). The updated
    signature map is returned as the scope's next position.

    Returns ``(events, next_position, truncated)`` for signature-compatibility with
    the time-series readers; ``truncated`` is always ``False`` because the rule set
    is bounded by construction.
    """
    seen = _decode_rule_state(watermark)
    new_state: Dict[str, str] = {}
    events: List[Dict[str, Any]] = []
    token: Optional[str] = None
    for _ in range(MAX_PAGES_PER_POLL):
        params: Dict[str, Any] = {"Limit": page_size}
        if token:
            params["NextToken"] = token
        resp = client.list_rules(**params) or {}
        for rule in resp.get("Rules", []) or []:
            detail = _describe_rule(client, rule)
            key = detail.get("Arn") or detail.get("Name") or ""
            if not key:
                continue
            sig = _rule_signature(detail)
            new_state[str(key)] = sig
            if seen.get(str(key)) == sig:
                continue  # unchanged rule — not re-read (AC3)
            events.append(_rule_to_event(detail, account_id=account_id, region=region))
        token = resp.get("NextToken")
        if not token:
            break
    else:
        logger.warning("aws_poll_source: eventbridge hit MAX_PAGES_PER_POLL for account %s", account_id)
    return events, json.dumps(new_state, sort_keys=True, separators=(",", ":")), False


def _decode_rule_state(watermark: str) -> Dict[str, str]:
    """Decode the EventBridge per-scope ``{rule_key: signature}`` checkpoint (tolerant)."""
    if not watermark:
        return {}
    try:
        data = json.loads(watermark)
    except (TypeError, ValueError):
        return {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def _rule_signature(rule: Dict[str, Any]) -> str:
    """Stable short fingerprint of a rule's identity + configuration.

    Changes when the rule's ARN, enabled state, event pattern, schedule, or
    description changes — so a modified rule re-emits while an untouched one does
    not. Excludes nothing time-based (rules carry no timestamp).
    """
    basis = "|".join(
        str(rule.get(k, ""))
        for k in ("Arn", "State", "EventPattern", "ScheduleExpression", "Description")
    )
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


def _describe_rule(client: Any, rule: Dict[str, Any]) -> Dict[str, Any]:
    """Enrich a ListRules entry with DescribeRule detail (tolerant; falls back)."""
    name = rule.get("Name")
    if not name or not hasattr(client, "describe_rule"):
        return rule
    try:
        detail = client.describe_rule(Name=name) or {}
    except Exception:  # noqa: BLE001 — a describe failure degrades to the list entry
        logger.debug("aws_poll_source: describe_rule failed for %s — using list entry", name, exc_info=True)
        return rule
    merged = dict(rule)
    merged.update(detail)
    return merged


def _rule_to_event(
    rule: Dict[str, Any], *, account_id: str, region: Optional[str]
) -> Dict[str, Any]:
    """Adapt an EventBridge rule to the map_eventbridge event envelope shape."""
    arn = rule.get("Arn") or ""
    name = rule.get("Name") or "EventBridge Rule"
    return {
        "id": arn or f"eventbridge-rule:{account_id}:{name}",
        "detail-type": name,
        "source": "aws.events",
        "account": account_id,
        "region": region,
        "resources": [arn] if arn else [],
        "detail": {"state": rule.get("State")},
    }


_SURFACE_READERS = {
    SURFACE_CLOUDWATCH: read_cloudwatch,
    SURFACE_EVENTBRIDGE: read_eventbridge,
    SURFACE_CLOUDTRAIL: read_cloudtrail,
}


class _ThrottleRetryingClient:
    """Wraps a boto3-shaped client so EACH API call retries its own throttling.

    Why per call rather than per read: a surface reader follows the provider's
    pagination internally, so retrying at the reader level throws away every page
    already fetched and re-reads them — under sustained throttling (``LookupEvents``
    allows only a couple of calls a second, so throttling is routine, not
    exceptional) the work becomes quadratic and the poll appears to hang. Retrying
    the individual call keeps page progress, which is also what "back off and report;
    do not thin the data quietly" (MSP-B1 failure posture) actually requires.

    Non-throttle errors propagate untouched, so the caller still reports the scope
    failed. Every back-off is counted in run health (never silent).
    """

    def __init__(
        self,
        client: Any,
        *,
        account_id: str,
        surface: str,
        health: AWSConnectorHealth,
        max_retries: int,
        sleeper: Callable[[float], None],
    ) -> None:
        self._client = client
        self._account_id = account_id
        self._surface = surface
        self._health = health
        self._max_retries = max_retries
        self._sleeper = sleeper
        #: Total back-offs performed across every call made through this wrapper.
        self.throttle_retries = 0

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._client, name)
        if not callable(attr):
            return attr

        def _retrying(*args: Any, **kwargs: Any) -> Any:
            attempt = 0
            while True:
                try:
                    return attr(*args, **kwargs)
                except Exception as exc:  # noqa: BLE001 — only throttling is retried
                    if not is_throttle_error(exc) or attempt >= self._max_retries:
                        raise
                    attempt += 1
                    self.throttle_retries += 1
                    self._health.record_throttle(
                        self._account_id, self._surface, self.throttle_retries
                    )
                    self._sleeper(_backoff_seconds(attempt))

        return _retrying


# ─────────────────────────────────────────────────────────────────────────────
# The poll source
# ─────────────────────────────────────────────────────────────────────────────

class AWSLivePollSource(CloudPollSource):
    """Live AWS :class:`CloudPollSource` — one connection, many accounts (AT-642).

    Builds a scope per ``(account, region, surface)`` from the managed-account
    config, authenticates each account through :class:`AWSAuthenticator`, and reads
    that surface with the minimal read-only API calls. Injectable authenticator
    (hence injectable client factory) so the whole path is exercised in tests with
    seeded fake clients — no boto3, no AWS account.
    """

    def __init__(
        self,
        accounts: List[AWSAccountConfig],
        authenticator: AWSAuthenticator,
        *,
        surfaces: Tuple[str, ...] = AWS_SURFACES,
        page_size: int = DEFAULT_PAGE_SIZE,
        max_throttle_retries: int = DEFAULT_MAX_THROTTLE_RETRIES,
        sleeper: Optional[Callable[[float], None]] = None,
        max_backfill_days: Optional[float] = None,
    ) -> None:
        for surface in surfaces:
            if surface not in AWS_SURFACE_MAPPERS:
                raise ValueError(f"unknown AWS surface {surface!r}")
        self.accounts = list(accounts)
        self.authenticator = authenticator
        self.surfaces = tuple(surfaces)
        self.page_size = page_size
        self.max_throttle_retries = max_throttle_retries
        self._sleeper = sleeper or time.sleep
        #: Initial-backfill DEPTH in days (0 = unbounded); see
        #: :data:`DEFAULT_MAX_BACKFILL_DAYS` and :func:`advance_descending`.
        self.max_backfill_days = (
            _configured_max_backfill_days()
            if max_backfill_days is None
            else max(0.0, float(max_backfill_days))
        )
        self._by_account: Dict[str, AWSAccountConfig] = {a.account_id: a for a in self.accounts}
        #: Per-account run-health surface (AT-646) — the R18-C2 connector-panel
        #: artifact, accumulated as scopes are polled. Loud, never silent.
        self.health = AWSConnectorHealth()
        #: Accounts whose successful authentication has already been logged, so the
        #: "authenticated" line appears once per account rather than once per scope
        #: (an account is polled once per surface/region). Observability only.
        self._authenticated_accounts: set = set()

    @property
    def max_backfill_seconds(self) -> float:
        """Initial-backfill depth in seconds (``0.0`` = unbounded walk)."""
        return self.max_backfill_days * 86400.0

    def health_report(self) -> Dict[str, Any]:
        """Per-account run-health report (auth/throttle/partial states)."""
        return self.health.to_dict()

    def list_scopes(self, org_id: str) -> List[CloudScope]:
        scopes: List[CloudScope] = []
        for account in self.accounts:
            regions = account.regions or (None,)
            for region in regions:
                for surface in self.surfaces:
                    scopes.append(aws_scope(
                        account.account_id, surface, region=region, label=account.label
                    ))
        return scopes

    def poll(self, org_id: str, scope: CloudScope, position: str) -> PollPage:
        account = self._by_account.get(scope.account)
        if account is None:  # scope for an account we do not manage — nothing to poll
            return PollPage(events=[], next_position=position, has_more=False)

        # Authenticate this account (assume-role, else direct keys). A per-account
        # auth failure is recorded LOUDLY in run health (never a silent skip) and
        # degrades only THIS account's scope to empty — other accounts continue (AC8).
        try:
            credentials: AWSCredentials = self.authenticator.credentials_for(org_id, account)
        except Exception as exc:  # noqa: BLE001 — degrade this account, don't crash the run
            self.health.mark_auth_failed(scope.account, f"{type(exc).__name__}: {exc}")
            return PollPage(events=[], next_position=position, has_more=False)

        # Runtime visibility: report the authentication OUTCOME and how the account
        # was reached (assumed role vs direct keys) exactly once per account per run.
        # Failures already log at WARNING via AWSConnectorHealth; without this the
        # success path was invisible, so "did AWS authenticate at all?" could only be
        # answered from the health report after the fact. Never logs the credential.
        if scope.account not in self._authenticated_accounts:
            self._authenticated_accounts.add(scope.account)
            logger.info(
                "aws_poll_source: authenticated account %s via %s (partition=%s)",
                scope.account,
                getattr(credentials, "source", "unknown"),
                account.partition,
            )

        service = _SERVICE_FOR_SURFACE[scope.surface]
        reader = _SURFACE_READERS[scope.surface]
        reader_kwargs: Dict[str, Any] = {}
        if scope.surface == SURFACE_CLOUDTRAIL:
            # Bound the DEPTH of an initial backfill so a first load on an account
            # with months of audit history converges (see aws_watermark).
            reader_kwargs["max_backfill_seconds"] = self.max_backfill_seconds

        # Read with throttle back-off: on a rate-limit error the individual API call
        # backs off and retries (counted in run health, page progress preserved)
        # rather than thinning the data. Any other error — or throttling that
        # outlasts the retry budget — is reported as a failed scope (loud), not
        # silently dropped.
        try:
            # Pass the account's CONFIGURED partition (AT-645): a GovCloud
            # connection must resolve aws-us-gov endpoints even when the scope
            # carries no explicit region, instead of silently falling back to
            # commercial endpoints derived from an absent region.
            client = self.authenticator.client_factory.client(
                service,
                region=scope.region,
                credentials=credentials,
                partition=account.partition,
            )
            events, new_position, has_more = reader(
                _ThrottleRetryingClient(
                    client,
                    account_id=scope.account,
                    surface=scope.surface,
                    health=self.health,
                    max_retries=self.max_throttle_retries,
                    sleeper=self._sleeper,
                ),
                region=scope.region,
                account_id=scope.account,
                watermark=position or "",
                page_size=self.page_size,
                **reader_kwargs,
            )
            self.health.mark_scope_ok(scope.account, scope.surface)
            # Per-surface visibility: which AWS surface was read, for which
            # account/region, and how many events it returned. This is the log
            # line that answers "is CloudWatch/EventBridge/CloudTrail actually
            # being polled?" — previously only failures were observable, so a
            # surface returning nothing looked identical to one never polled.
            logger.info(
                "aws_poll_source: polled %s account=%s region=%s — %d event(s)%s",
                scope.surface,
                scope.account,
                scope.region or "-",
                len(events),
                " (more pending)" if has_more else "",
            )
            # Pagination up to MAX_PAGES_PER_POLL happens inside the reader.
            # Beyond that the reader reports has_more so the skeleton polls this
            # scope again from the advanced position — a large backlog is drained
            # across several polls instead of being silently truncated. How many
            # such continuation polls one RUN performs is bounded by the skeleton
            # (poll cap / deadline / B7 budget), which reports the undrained
            # remainder loudly and resumes it next run.
            return PollPage(
                events=events, next_position=new_position, has_more=has_more
            )
        except Exception as exc:  # noqa: BLE001 — loud per-scope failure, not silent
            # Throttling was already retried per API call inside
            # _ThrottleRetryingClient (which preserves page progress); reaching here
            # means the retry budget was exhausted or the failure was not a throttle.
            self.health.mark_scope_failed(
                scope.account, scope.surface, f"{type(exc).__name__}: {exc}"
            )
            return PollPage(events=[], next_position=position, has_more=False)


def _backoff_seconds(attempt: int) -> float:
    """Exponential back-off (capped) for the Nth throttle retry."""
    return min(_BACKOFF_CAP_SECONDS, _BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))


def build_live_aws_source(
    *,
    accounts: Optional[List[AWSAccountConfig]] = None,
    authenticator: Optional[AWSAuthenticator] = None,
    surfaces: Tuple[str, ...] = AWS_SURFACES,
    page_size: int = DEFAULT_PAGE_SIZE,
    env: Optional[Dict[str, str]] = None,
) -> AWSLivePollSource:
    """Build a live AWS poll source from config (accounts) + a default authenticator.

    ``accounts`` defaults to :func:`aws_auth.load_aws_accounts` (the
    ``AWS_EVENT_ACCOUNTS`` config); ``authenticator`` defaults to a vault-backed
    :class:`AWSAuthenticator` with the real boto3 client factory. Both are
    injectable so tests build a fully-seeded source with no AWS account.
    """
    from .aws_auth import load_aws_accounts

    accts = accounts if accounts is not None else load_aws_accounts(env=env)
    auth = authenticator or AWSAuthenticator()
    return AWSLivePollSource(accts, auth, surfaces=surfaces, page_size=page_size)
