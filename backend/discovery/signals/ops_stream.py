"""MSP-B7 / AT-669 — dedup at admission (active-signal folding).

Cloud event streams are orders of magnitude noisier than any business system
AgentIQ reads: a stuck CloudWatch alarm re-fires every few minutes, an Azure
activity log floods, and the same fault reappears hundreds of times a day. If
every re-fire reached a detector as its own signal, findings would drown in
duplicates and per-run economics would collapse at cloud volumes.

This module is the **first discipline** of MSP-B7 — *deduplication at the door*.
Re-firing events collapse into **one active signal** per
``(event_signature, resource, active period)`` carrying an **occurrence count**
and **first/last timestamps**. A stuck alarm firing every five minutes is ONE
fact with a count of 288/day — not 288 facts.

The honesty rule (MSP-B7)
-------------------------
An aggregate is a claim about many events, so it carries its proof: the member
count, the time span, and — because the raw provider payloads stay stored
(MSP-B0 / AT-638) — the provider event ids of every folded firing. ``'this alarm
fired 200 times'`` opens back to the real instances on click via
:meth:`ActiveSignal.resolve_raw_instances`. Aggregation is compression of
*volume*, never compression of *evidence*.

Determinism & org-scoping (the two hard requirements)
-----------------------------------------------------
* **Deterministic** — folding depends only on event fields, never on arrival
  order. The active period is an epoch-anchored time bucket (default daily), the
  count is over *distinct* provider event ids, first/last are the min/max
  observation times, and the detector-visible representative is the earliest
  firing by ``(observed_at, signal_id)``. Admitting the same set of events in any
  order yields the identical active signals.
* **Org-scoped** — the fold key includes ``org_id``, so two orgs whose events
  share a signature never fold together. :meth:`OpsEventStream.admit` refuses to
  admit an event under an ``org_id`` other than the one the event owns.

Scope
-----
This module owns MSP-B7 **T1** (the dedup fold) and hosts **T4** (the per-run
event-volume budget enforced inside :meth:`OpsEventStream.admit` — see
:mod:`discovery.signals.budget`), since a budget must be applied while events are
being admitted. Aggregation roll-ups for high-cardinality classes (T2,
:mod:`discovery.signals.aggregation`), noise floors (T3,
:mod:`discovery.signals.noise_floor`), and correlation windows (T5) are separate
modules that layer on top of the folded active signals.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

try:
    from app.provenance import EvidencePointer
except ModuleNotFoundError:  # project-root execution uses backend as package
    from backend.app.provenance import EvidencePointer

from .budget import BudgetReport, RunBudget
from .evidence_store import OrgScopeError, RawEventStore
from .operational_event import OperationalEvent

logger = logging.getLogger(__name__)

#: Default length of an *active period* in seconds. One UTC day: a stuck alarm
#: re-firing all day collapses to one active signal per day (the "288/day"
#: cadence in the MSP-B7 story). Anchored to the Unix epoch, so bucket boundaries
#: fall on UTC midnight and the bucket is a pure function of the timestamp —
#: order-independent and deterministic. Tunable per stream (calibration is T6).
DEFAULT_ACTIVE_PERIOD_SECONDS = 86_400

#: Sentinel active-period key for an event whose ``observed_at`` cannot be parsed.
#: Such events still fold by ``(org, signature, resource)`` and never crash
#: admission — they simply do not contribute to the time span.
_UNBUCKETED = "unbucketed"

# (org_id, event_signature, resource_id, active_period_key)
_FoldKey = Tuple[str, str, str, str]


def _parse_iso(ts: Optional[str]) -> Optional[datetime]:
    """Parse a UTC ISO-8601 timestamp to an aware datetime, tolerantly.

    Accepts a trailing ``Z`` and naive timestamps (assumed UTC). Returns ``None``
    for an empty or unparseable value so the caller can degrade rather than raise
    — a malformed timestamp must never break admission.
    """
    if not ts:
        return None
    s = str(ts).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _active_period_key(dt: Optional[datetime], period_seconds: int) -> str:
    """Deterministic active-period bucket key for an observation time.

    ``floor(epoch / period_seconds)`` indexes the bucket; the key is the bucket's
    UTC start as an ISO string. Pure function of the timestamp, so it never
    depends on admission order. Unparseable times fall in :data:`_UNBUCKETED`.
    """
    if dt is None:
        return _UNBUCKETED
    idx = int(dt.timestamp() // period_seconds)
    start = datetime.fromtimestamp(idx * period_seconds, tz=timezone.utc)
    return start.isoformat()


# ─────────────────────────────────────────────────────────────────────────────
# Active signal — the folded, detector-visible unit
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ActiveSignal:
    """One folded signal for a recurring event within an active period.

    The detector-visible unit that many re-fires collapse into. It carries the
    aggregate's proof — ``occurrence_count`` (distinct firings), the
    ``first_seen``/``last_seen`` span, and every folded firing's provider event
    id — alongside the ``representative`` normalised event a detector reasons
    over. The raw provider payloads are NOT embedded (they stay in the raw-event
    store, MSP-B0 / AT-638); :meth:`resolve_raw_instances` walks each member's
    evidence pointer back to its stored raw payload.
    """

    org_id: str
    event_signature: str
    resource_id: str
    active_period_start: str
    representative: OperationalEvent
    occurrence_count: int
    first_seen: str
    last_seen: str
    #: Provider event ids of every folded firing, in first-admitted order (a set,
    #: exposed sorted() in to_dict for order-independence). Retained for trace-back.
    provider_event_ids: List[str] = field(default_factory=list)
    #: One evidence pointer per folded firing — each resolves to a stored raw payload.
    member_pointers: List[Dict[str, Any]] = field(default_factory=list)
    #: Distribution of member severities (severity token → distinct-firing count).
    #: The signature deliberately ignores severity (AT-636), so one recurring event
    #: may span severities; this profile preserves that spread for the MSP-B7 T2
    #: aggregate roll-up. Order-independent (a count map).
    severity_profile: Dict[str, int] = field(default_factory=dict)
    #: Earliest/latest observation time as aware datetimes (span comparison state).
    _first_dt: Optional[datetime] = field(default=None, repr=False)
    _last_dt: Optional[datetime] = field(default=None, repr=False)

    @property
    def is_recurrence(self) -> bool:
        """True when more than one distinct firing folded into this signal."""
        return self.occurrence_count > 1

    def to_dict(self) -> Dict[str, Any]:
        """JSON-serialisable detector-visible mapping.

        The representative event's normalised shape plus the recurrence proof
        (count, span, sorted provider ids). ``provider_event_ids`` is exposed
        sorted so the mapping is order-independent (a determinism guarantee).
        """
        data = self.representative.to_dict()
        data.update(
            occurrence_count=self.occurrence_count,
            first_seen=self.first_seen,
            last_seen=self.last_seen,
            active_period_start=self.active_period_start,
            is_recurrence=self.is_recurrence,
            provider_event_ids=sorted(self.provider_event_ids),
            severity_profile=dict(self.severity_profile),
        )
        return data

    def resolve_raw_instances(
        self, store: RawEventStore, org_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Resolve every folded firing back to its stored raw provider payload.

        Walks each member's OBSERVED evidence pointer ``(source_system,
        source_artifact)`` and reads the raw payload from ``store`` within the
        signal's org. This is the "opens to real instances on click" guarantee:
        an aggregate never loses the evidence behind it. Members with nothing
        stored (raw persistence skipped) are omitted. Refuses to cross an org
        boundary (raw evidence is hard-partitioned, MSP-B0 / AT-638).
        """
        scope = org_id or self.org_id
        if scope != self.org_id:
            raise OrgScopeError(
                f"signal belongs to org {self.org_id!r}, cannot resolve under {scope!r}"
            )
        raws: List[Dict[str, Any]] = []
        for pointer in self.member_pointers:
            p = EvidencePointer.from_dict(pointer)
            raw = store.get(self.org_id, p.source_system, p.source_artifact)
            if raw is not None:
                raws.append(raw)
        return raws


@dataclass
class Admission:
    """The outcome of admitting one event.

    ``signal`` is the (live) active signal the event belongs to, or ``None`` when
    the event was deferred (no signal is formed). ``disposition`` is:

    * ``"new"`` — opened a fresh active signal;
    * ``"folded"`` — a re-fire folded into an existing one;
    * ``"duplicate"`` — an at-least-once redelivery of a firing already counted
      (no state change, so admission stays idempotent);
    * ``"deferred"`` — the run's event budget was exhausted, so the event was
      deferred-and-counted rather than processed (MSP-B7 T4). Never silent.
    """

    signal: Optional[ActiveSignal]
    disposition: str

    @property
    def is_new(self) -> bool:
        return self.disposition == "new"

    @property
    def folded(self) -> bool:
        return self.disposition == "folded"

    @property
    def is_duplicate(self) -> bool:
        return self.disposition == "duplicate"

    @property
    def is_deferred(self) -> bool:
        return self.disposition == "deferred"


# ─────────────────────────────────────────────────────────────────────────────
# The admission stream
# ─────────────────────────────────────────────────────────────────────────────

class OpsEventStream:
    """Folds re-firing operational events into active signals at admission.

    Stateful over the lifetime of a run: :meth:`admit` looks up the active signal
    for an event's ``(org_id, event_signature, resource, active period)`` and
    either opens a new one or folds the event in (incrementing the count,
    extending the span, retaining the provider id). One instance can serve many
    orgs safely — the fold key includes ``org_id``, so orgs never fold together.

    ``active_period_seconds`` sets the active-period bucket length (default one
    day); tune it per stream (MSP-B7 T6 calibrates the defaults). ``budget`` sets
    the per-run event-volume budget (MSP-B7 T4): once that many events have been
    processed, further events are deferred-and-counted rather than folded, and
    :meth:`budget_report` reports the deferred volume. ``None`` (default) means
    no budget — every event is processed.
    """

    def __init__(
        self,
        *,
        active_period_seconds: int = DEFAULT_ACTIVE_PERIOD_SECONDS,
        budget: Optional[int] = None,
    ):
        if active_period_seconds <= 0:
            raise ValueError("active_period_seconds must be positive")
        self._active_period_seconds = int(active_period_seconds)
        self._signals: Dict[_FoldKey, ActiveSignal] = {}
        self._budget = RunBudget(budget)

    # -- fold key ------------------------------------------------------------

    def _fold_key(
        self, event: OperationalEvent, dt: Optional[datetime]
    ) -> Tuple[_FoldKey, str]:
        """Compute the deterministic fold key for an event (dt pre-parsed).

        Returns the key plus the resolved active-period start.
        """
        period_key = _active_period_key(dt, self._active_period_seconds)
        resource_id = event.resource.resource_id if event.resource else ""
        key: _FoldKey = (event.org_id, event.event_signature, resource_id, period_key)
        return key, period_key

    # -- admission -----------------------------------------------------------

    def admit(self, event: OperationalEvent, *, org_id: Optional[str] = None) -> Admission:
        """Admit one operational event, folding re-fires into an active signal.

        Deterministic and org-scoped (see the module docstring). A re-fire — a
        distinct provider event id sharing an existing signal's ``(org,
        signature, resource, active period)`` — increments the count and extends
        the span. A redelivery of an already-counted firing is idempotent
        (``disposition == "duplicate"``). The first firing of a key opens a new
        active signal. When the run's event budget is exhausted the event is
        deferred-and-counted instead of processed (``disposition == "deferred"``,
        MSP-B7 T4) — never silently dropped.

        Args:
            event:  the normalised operational event to admit.
            org_id: optional explicit org for a boundary check; defaults to the
                    event's own ``org_id``. Passing a different org raises
                    :class:`OrgScopeError` (events never cross org boundaries).
        """
        if not isinstance(event, OperationalEvent):
            raise TypeError("admit expects an OperationalEvent")
        scope = org_id or event.org_id
        if scope != event.org_id:
            raise OrgScopeError(
                f"event belongs to org {event.org_id!r}, cannot admit under {scope!r}"
            )

        dt = _parse_iso(event.observed_at)

        # Per-run budget (T4): once the budgeted window is full, defer-and-count
        # every further event rather than processing it — loud, never silent.
        if not self._budget.has_capacity():
            self._budget.defer(event.source_system, event.observed_at, dt)
            return Admission(None, "deferred")

        key, period_key = self._fold_key(event, dt)
        existing = self._signals.get(key)
        if existing is None:
            self._signals[key] = self._open(event, period_key, dt)
            self._budget.charge()
            return Admission(self._signals[key], "new")

        # An at-least-once redelivery of a firing already counted — idempotent.
        if event.signal_id in existing.provider_event_ids:
            self._budget.charge()
            return Admission(existing, "duplicate")

        self._fold(existing, event, dt)
        self._budget.charge()
        return Admission(existing, "folded")

    def _open(
        self, event: OperationalEvent, period_key: str, dt: Optional[datetime]
    ) -> ActiveSignal:
        """Open a fresh active signal for the first firing of a fold key."""
        ts = event.observed_at
        return ActiveSignal(
            org_id=event.org_id,
            event_signature=event.event_signature,
            resource_id=event.resource.resource_id if event.resource else "",
            active_period_start=period_key,
            representative=event,
            occurrence_count=1,
            first_seen=ts,
            last_seen=ts,
            provider_event_ids=[event.signal_id],
            member_pointers=[dict(event.provenance)],
            severity_profile={event.severity: 1},
            _first_dt=dt,
            _last_dt=dt,
        )

    def _fold(self, signal: ActiveSignal, event: OperationalEvent, dt: Optional[datetime]) -> None:
        """Fold a distinct re-fire into an existing active signal.

        Order-independent: count is over distinct ids, the span is min/max of
        observation times, and the representative is re-selected as the earliest
        firing by ``(observed_at, signal_id)`` — so the folded result never
        depends on the order firings arrived.
        """
        signal.occurrence_count += 1
        signal.provider_event_ids.append(event.signal_id)
        signal.member_pointers.append(dict(event.provenance))
        signal.severity_profile[event.severity] = (
            signal.severity_profile.get(event.severity, 0) + 1
        )

        # Span maintenance — only parseable times move the span (an unparseable
        # timestamp still counts as a firing but cannot extend first/last).
        if dt is not None:
            if signal._first_dt is None or dt < signal._first_dt:
                signal._first_dt, signal.first_seen = dt, event.observed_at
            if signal._last_dt is None or dt > signal._last_dt:
                signal._last_dt, signal.last_seen = dt, event.observed_at

        # Deterministic representative: the earliest firing by (observed_at,
        # signal_id). Re-selected on every fold so admission order is irrelevant.
        if self._precedes(event, signal.representative):
            signal.representative = event

    @staticmethod
    def _precedes(a: OperationalEvent, b: OperationalEvent) -> bool:
        """True if ``a`` sorts before ``b`` by (observed_at, signal_id)."""
        da, db = _parse_iso(a.observed_at), _parse_iso(b.observed_at)
        # Unparseable times sort last so a valid firing is always preferred as
        # the representative; ties fall through to the stable signal_id.
        ka = (da is None, da or datetime.max.replace(tzinfo=timezone.utc), a.signal_id)
        kb = (db is None, db or datetime.max.replace(tzinfo=timezone.utc), b.signal_id)
        return ka < kb

    # -- read side -----------------------------------------------------------

    def active_signals(self, org_id: Optional[str] = None) -> List[ActiveSignal]:
        """The detector-visible active signals, optionally filtered to one org.

        Returned deterministically ordered by ``(event_signature, resource_id,
        active_period_start)`` so the collection is independent of admission
        order.
        """
        signals = [
            s for s in self._signals.values()
            if org_id is None or s.org_id == org_id
        ]
        signals.sort(key=lambda s: (s.event_signature, s.resource_id, s.active_period_start))
        return signals

    def has_capacity(self) -> bool:
        """True while the run's event budget can still process another event.

        The read side of MSP-B7 T4's budget, exposed so a POLLING producer can stop
        *fetching* once the budgeted window is full instead of paying for provider
        pages whose events :meth:`admit` will only defer. The budget's purpose is to
        stop the run processing everything (see :mod:`discovery.signals.budget`);
        without this a producer with an unbounded backlog keeps calling the provider
        forever while every event is deferred — the budget bounds the data but never
        the work. An unbudgeted stream always has capacity.
        """
        return self._budget.has_capacity()

    def budget_report(self) -> BudgetReport:
        """The run's event-budget outcome (MSP-B7 T4).

        A snapshot of how many events were processed vs deferred, the per-source
        deferred breakdown, and the deferred time window — the loud-degradation
        proof written into the run record / R18-C2 run-health panel. When no
        budget was set (or it was never breached) ``breached`` is ``False``.
        """
        return self._budget.snapshot()


# ─────────────────────────────────────────────────────────────────────────────
# Batch convenience
# ─────────────────────────────────────────────────────────────────────────────

def fold_events(
    events, *, org_id: Optional[str] = None,
    active_period_seconds: int = DEFAULT_ACTIVE_PERIOD_SECONDS,
) -> List[ActiveSignal]:
    """Admit an iterable of events and return the resulting active signals.

    Convenience over :class:`OpsEventStream` for callers that hold the whole
    batch: builds a stream, admits every event, and returns
    ``stream.active_signals(org_id)``. Deterministic — the result is independent
    of the iteration order.
    """
    stream = OpsEventStream(active_period_seconds=active_period_seconds)
    for event in events:
        stream.admit(event, org_id=org_id)
    return stream.active_signals(org_id)
