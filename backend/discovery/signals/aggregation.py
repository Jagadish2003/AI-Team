"""MSP-B7 / AT-670 — aggregation roll-ups for high-cardinality event classes.

Dedup at the door (MSP-B7 T1, :mod:`discovery.signals.ops_stream`) already folds
re-firing events into one active signal per ``(event_signature, resource, active
period)``. This module is the **second discipline** — *aggregation with
traceability* — for the high-cardinality classes that flood at cloud volumes:
**audit floods** and **state-change storms**. Their folded active signals are
rolled up into a compact :class:`AggregateSignal` that becomes the
**detector-visible unit**, so a detector reasons about *"this audit action fired
9 000 times this window"* as ONE fact instead of drowning in 9 000.

The honesty rule (MSP-B7)
-------------------------
An aggregate is a claim about many events, so it carries its proof — the exact
**member count**, the **time span**, a **severity profile** (the spread the
signature deliberately ignores), and **sampled evidence pointers**. The raw
provider payloads stay stored untouched (MSP-B0 / AT-638 — "raw retention
unchanged"); the aggregate holds only a *bounded sample* of pointers, and each
one resolves back to a real stored payload via
:meth:`AggregateSignal.resolve_sample_raw`. Aggregation is compression of
*volume*, never compression of *evidence* — ``'this fired 9 000 times'`` still
opens to real instances on click.

Determinism & org-scoping
--------------------------
The roll-up is a pure projection of the (already deterministic, org-scoped) T1
active signals: the count and span come straight through, the severity profile is
a count map, and the evidence sample is chosen by a deterministic rule (sorted by
``(source_timestamp, source_artifact)``, then evenly spaced **including both span
endpoints**) so the same members always yield the same sample regardless of
arrival order. Org isolation rides on the underlying active signal's ``org_id``.

Scope (T2 only)
---------------
This is MSP-B7 **T2**. Noise floors (T3), per-run budgets (T4), and correlation
windows (T5) are separate tasks. The high-cardinality class set and the sample
size are configurable here; their calibrated defaults are set from B8's
month-scale measurements in T6.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

try:
    from app.provenance import EvidencePointer
except ModuleNotFoundError:  # project-root execution uses backend as package
    from backend.app.provenance import EvidencePointer

from .evidence_store import OrgScopeError, RawEventStore
from .operational_event import SEVERITY_ORDER
from .ops_stream import DEFAULT_ACTIVE_PERIOD_SECONDS, ActiveSignal, OpsEventStream

#: The high-cardinality event classes that flood at cloud volumes and are rolled
#: up into aggregates — the "audit floods" and "state-change storms" the MSP-B7
#: story names. Tunable per call; T6 calibrates the default set from B8's
#: measured month-scale sample.
HIGH_CARDINALITY_CLASSES: frozenset = frozenset({"audit", "state_change"})

#: Default cap on evidence pointers retained per aggregate. The member *count* is
#: always exact; only the retained *pointers* are bounded, so a flood costs a
#: constant amount of pointer storage regardless of volume. Tunable per call.
DEFAULT_EVIDENCE_SAMPLE_SIZE = 10


def _sample_indices(n: int, k: int) -> List[int]:
    """Deterministically choose up to ``k`` indices from ``range(n)``.

    Always includes the first and last index (the span endpoints — the sample
    must be able to open to the earliest and latest instance), then spreads the
    remainder evenly. Pure function of ``(n, k)`` — no randomness — so the sample
    is reproducible.
    """
    if n <= 0 or k <= 0:
        return []
    if n <= k:
        return list(range(n))
    if k == 1:
        return [0]
    # Evenly spaced across [0, n-1] inclusive of both endpoints.
    step = (n - 1) / (k - 1)
    idx = sorted({int(round(i * step)) for i in range(k)})
    # Rounding collisions can drop below k; top up with unused indices to keep
    # the sample as full as the bound allows, deterministically.
    if len(idx) < k:
        for cand in range(n):
            if cand not in idx:
                idx.append(cand)
                if len(idx) == k:
                    break
        idx.sort()
    return idx


def _sample_pointers(
    pointers: List[Dict[str, Any]], sample_size: int
) -> List[Dict[str, Any]]:
    """Return a deterministic, bounded, span-anchored sample of evidence pointers.

    Members are first ordered by ``(source_timestamp, source_artifact)`` so the
    sample is independent of admission order, then :func:`_sample_indices` picks
    the bounded set (endpoints always included). Each returned pointer is a real
    member pointer that resolves to a stored raw payload.
    """
    ordered = sorted(
        pointers,
        key=lambda p: (str(p.get("source_timestamp") or ""),
                       str(p.get("source_artifact") or "")),
    )
    return [ordered[i] for i in _sample_indices(len(ordered), sample_size)]


@dataclass
class AggregateSignal:
    """A rolled-up, detector-visible aggregate over a high-cardinality signal.

    Carries the aggregate's proof — the exact ``member_count``, the
    ``first_seen``/``last_seen`` span, the ``severity_profile`` spread, and a
    bounded ``sample_pointers`` set that resolves to stored raw payloads
    (AT-638). ``sampled_from`` records the true member count the sample was drawn
    from, so the compression ratio is never hidden.
    """

    org_id: str
    event_signature: str
    resource_id: str
    window_start: str
    event_class: str
    resource_type: str
    member_count: int
    first_seen: str
    last_seen: str
    severity_profile: Dict[str, int]
    #: Bounded sample of member evidence pointers — each resolves to a raw payload.
    sample_pointers: List[Dict[str, Any]] = field(default_factory=list)
    #: The true number of members the sample was drawn from (== member_count).
    sampled_from: int = 0
    #: The detector-visible representative event (the earliest firing).
    representative: Optional[Any] = None

    @property
    def is_sampled(self) -> bool:
        """True when the retained pointers are a strict sample of the members."""
        return len(self.sample_pointers) < self.sampled_from

    def to_dict(self) -> Dict[str, Any]:
        """JSON-serialisable mapping of the aggregate's detector-visible proof.

        ``severity_profile`` is ordered most-severe-first (via
        :data:`~discovery.signals.operational_event.SEVERITY_ORDER`) for a stable,
        order-independent serialisation.
        """
        profile = {
            sev: self.severity_profile[sev]
            for sev in sorted(
                self.severity_profile,
                key=lambda s: SEVERITY_ORDER.get(s, -1),
                reverse=True,
            )
        }
        return {
            "org_id": self.org_id,
            "event_signature": self.event_signature,
            "resource_id": self.resource_id,
            "window_start": self.window_start,
            "event_class": self.event_class,
            "resource_type": self.resource_type,
            "member_count": self.member_count,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "severity_profile": profile,
            "sample_size": len(self.sample_pointers),
            "sampled_from": self.sampled_from,
            "is_sampled": self.is_sampled,
        }

    def resolve_sample_raw(
        self, store: RawEventStore, org_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Resolve every sampled pointer back to its stored raw provider payload.

        The traceable-aggregate guarantee (AC2): each sample pointer opens to a
        real stored instance. Refuses to cross an org boundary (raw evidence is
        hard-partitioned, AT-638); omits any sample whose raw payload is not
        stored.
        """
        scope = org_id or self.org_id
        if scope != self.org_id:
            raise OrgScopeError(
                f"aggregate belongs to org {self.org_id!r}, cannot resolve under {scope!r}"
            )
        raws: List[Dict[str, Any]] = []
        for pointer in self.sample_pointers:
            p = EvidencePointer.from_dict(pointer)
            raw = store.get(self.org_id, p.source_system, p.source_artifact)
            if raw is not None:
                raws.append(raw)
        return raws


def aggregate_active_signal(
    signal: ActiveSignal, *, sample_size: int = DEFAULT_EVIDENCE_SAMPLE_SIZE
) -> AggregateSignal:
    """Roll one folded :class:`ActiveSignal` up into an :class:`AggregateSignal`.

    A pure projection: count, span, and severity profile pass straight through
    from the deterministic T1 fold; the evidence pointers are reduced to a
    bounded, span-anchored sample (raw payloads themselves are untouched).
    """
    rep = signal.representative
    return AggregateSignal(
        org_id=signal.org_id,
        event_signature=signal.event_signature,
        resource_id=signal.resource_id,
        window_start=signal.active_period_start,
        event_class=rep.event_class,
        resource_type=rep.resource_type,
        member_count=signal.occurrence_count,
        first_seen=signal.first_seen,
        last_seen=signal.last_seen,
        severity_profile=dict(signal.severity_profile),
        sample_pointers=_sample_pointers(signal.member_pointers, sample_size),
        sampled_from=signal.occurrence_count,
        representative=rep,
    )


def roll_up(
    active_signals: Iterable[ActiveSignal],
    *,
    high_cardinality_classes: frozenset = HIGH_CARDINALITY_CLASSES,
    sample_size: int = DEFAULT_EVIDENCE_SAMPLE_SIZE,
    only_high_cardinality: bool = True,
) -> List[AggregateSignal]:
    """Roll active signals up into aggregates for the high-cardinality classes.

    By default only signals whose event class is a flood/storm class
    (:data:`HIGH_CARDINALITY_CLASSES`) are rolled up — low-cardinality signals
    (e.g. individual alarms) stay as their full T1 active signals, keeping full
    traceability where volume allows. Pass ``only_high_cardinality=False`` to
    aggregate every signal. Deterministically ordered by ``(event_signature,
    resource_id, window_start)``.

    Args:
        active_signals: the T1 folded active signals (e.g.
            ``OpsEventStream.active_signals()``).
        high_cardinality_classes: the event classes to roll up (tunable; T6
            calibrates the default).
        sample_size: cap on evidence pointers retained per aggregate.
        only_high_cardinality: when True (default) skip signals outside the
            high-cardinality set.
    """
    aggregates = [
        aggregate_active_signal(s, sample_size=sample_size)
        for s in active_signals
        if not only_high_cardinality
        or s.representative.event_class in high_cardinality_classes
    ]
    aggregates.sort(key=lambda a: (a.event_signature, a.resource_id, a.window_start))
    return aggregates


def aggregate_events(
    events,
    *,
    org_id: Optional[str] = None,
    active_period_seconds: int = DEFAULT_ACTIVE_PERIOD_SECONDS,
    high_cardinality_classes: frozenset = HIGH_CARDINALITY_CLASSES,
    sample_size: int = DEFAULT_EVIDENCE_SAMPLE_SIZE,
    only_high_cardinality: bool = True,
) -> List[AggregateSignal]:
    """Admit an iterable of events and roll the result up into aggregates.

    Convenience over ``OpsEventStream`` + :func:`roll_up` for callers holding a
    whole batch: folds every event through admission (T1 dedup), then aggregates
    the high-cardinality active signals (T2). Deterministic — the result is
    independent of iteration order.
    """
    stream = OpsEventStream(active_period_seconds=active_period_seconds)
    for event in events:
        stream.admit(event, org_id=org_id)
    return roll_up(
        stream.active_signals(org_id),
        high_cardinality_classes=high_cardinality_classes,
        sample_size=sample_size,
        only_high_cardinality=only_high_cardinality,
    )
