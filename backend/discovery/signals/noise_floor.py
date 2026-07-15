"""MSP-B7 / AT-671 — noise floors per event class (loud suppression).

Dedup at the door (T1) folds re-fires; aggregation (T2) rolls up floods. This is
the **third discipline** — *noise floors*. Cloud streams carry vast low-value
chatter: one-off audit records, single state flips, access noise. A per-event-class
**floor** sets the minimum number of times a signature must recur within its
active period to be worth a detector's attention. A signature below its class floor
**never becomes a detector-visible signal**.

The loud-skip rule applied to noise
-----------------------------------
Suppression is a *decision*, not a silent drop. Every suppressed signature and
every suppressed event is **counted and reported per run**, per class, so
*"we ignored 40 000 info-class events across 12 000 one-off signatures"* is a
visible, tunable decision an operator can challenge — never a quiet data loss.
That is the MSP-B7 honesty rule ("loud everywhere") applied to the noise floor:
the pack compresses volume aggressively and hides nothing.

Where it sits in admission
--------------------------
Per the MSP-B7 pipeline (dedup → floor → budget → aggregate), the floor is applied
to the **folded** active signals — after T1, so each signature's occurrence count
is known, and before T2, so only survivors are rolled up and reach detectors. It
is a pure, deterministic, order-independent projection: partition the folded
signals into *visible* (count ≥ class floor) and *suppressed* (count < floor), and
tally the suppressed volume.

Scope (T3 only)
---------------
Floors are configurable per event class; their calibrated defaults come from B8's
month-scale measurements in T6. Security- and error-class signals are never
floored by default (you never silently drop a security finding). Per-run budgets
(T4) and correlation windows (T5) are separate tasks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

from .ops_stream import ActiveSignal

#: Pre-calibration per-event-class floors (minimum occurrence count within a
#: signature's active period to be detector-visible). These are the noisy,
#: high-chatter classes; a signature in one of them that recurs fewer than the
#: floor's worth of times in its window is treated as noise. Classes NOT listed
#: fall back to :data:`DEFAULT_FLOOR` (= 1 → never suppressed) — deliberately
#: including ``error`` and ``security``, which must never be silently dropped.
#: Tunable per policy; T6 sets the calibrated defaults from B8's real volumes.
DEFAULT_NOISE_FLOORS: Dict[str, int] = {
    "audit": 5,          # audit floods — an action recurring < 5× a window is chatter
    "state_change": 5,   # state-change storms
    "access": 5,         # access/API chatter
}

#: Floor for any event class not named in the active floor map. 1 means "surface
#: even a single occurrence" — i.e. no suppression unless a class is explicitly floored.
DEFAULT_FLOOR = 1


@dataclass
class SuppressionReport:
    """Per-run, per-class record of what the noise floor suppressed.

    The loud-skip proof: it names the ``floors`` that were applied and, per event
    class, how many distinct signatures and how many underlying events were
    suppressed. JSON-serialisable for the run record / run-health surface
    (R18-C2), so suppression is always visible and challengeable.
    """

    floors: Dict[str, int] = field(default_factory=dict)
    #: event class → number of distinct signatures suppressed.
    suppressed_signatures: Dict[str, int] = field(default_factory=dict)
    #: event class → total underlying event volume suppressed.
    suppressed_events: Dict[str, int] = field(default_factory=dict)

    def record(self, event_class: str, event_volume: int) -> None:
        """Tally one suppressed signature carrying ``event_volume`` events."""
        self.suppressed_signatures[event_class] = (
            self.suppressed_signatures.get(event_class, 0) + 1
        )
        self.suppressed_events[event_class] = (
            self.suppressed_events.get(event_class, 0) + event_volume
        )

    @property
    def total_suppressed_signatures(self) -> int:
        return sum(self.suppressed_signatures.values())

    @property
    def total_suppressed_events(self) -> int:
        return sum(self.suppressed_events.values())

    @property
    def any_suppressed(self) -> bool:
        return self.total_suppressed_signatures > 0

    def to_dict(self) -> Dict[str, object]:
        """The per-run report shape (per-class maps ordered by class for stability)."""
        return {
            "floors": dict(sorted(self.floors.items())),
            "suppressed_signatures": dict(sorted(self.suppressed_signatures.items())),
            "suppressed_events": dict(sorted(self.suppressed_events.items())),
            "total_suppressed_signatures": self.total_suppressed_signatures,
            "total_suppressed_events": self.total_suppressed_events,
        }


class NoiseFloorPolicy:
    """Per-event-class noise floors and the suppression they perform.

    ``floors`` overrides :data:`DEFAULT_NOISE_FLOORS`; any class absent from the
    resolved map uses ``default_floor``. A signature is suppressed when its
    occurrence count is strictly below its class floor.
    """

    def __init__(
        self,
        floors: Optional[Dict[str, int]] = None,
        *,
        default_floor: int = DEFAULT_FLOOR,
    ):
        if default_floor < 1:
            raise ValueError("default_floor must be >= 1")
        resolved = dict(DEFAULT_NOISE_FLOORS if floors is None else floors)
        for cls, floor in resolved.items():
            if floor < 1:
                raise ValueError(f"floor for {cls!r} must be >= 1, got {floor}")
        self._floors = resolved
        self._default_floor = int(default_floor)

    def floor_for(self, event_class: str) -> int:
        """The floor for an event class (its configured floor, else the default)."""
        return self._floors.get(event_class, self._default_floor)

    def is_below_floor(self, signal: ActiveSignal) -> bool:
        """True when ``signal`` recurs fewer times than its class floor requires."""
        return signal.occurrence_count < self.floor_for(signal.representative.event_class)

    def apply(
        self, signals: Iterable[ActiveSignal]
    ) -> Tuple[List[ActiveSignal], SuppressionReport]:
        """Partition folded signals into detector-visible and suppressed-and-counted.

        Returns ``(visible, report)`` where ``visible`` are the signals at or above
        their class floor (deterministically ordered by ``(event_signature,
        resource_id, active_period_start)``) and ``report`` tallies the suppressed
        signatures and their event volume per class. Pure and order-independent —
        the split depends only on each signal's own count.
        """
        report = SuppressionReport(floors=dict(self._floors))
        visible: List[ActiveSignal] = []
        for signal in signals:
            if self.is_below_floor(signal):
                report.record(signal.representative.event_class, signal.occurrence_count)
            else:
                visible.append(signal)
        visible.sort(
            key=lambda s: (s.event_signature, s.resource_id, s.active_period_start)
        )
        return visible, report


def apply_noise_floors(
    signals: Iterable[ActiveSignal], policy: Optional[NoiseFloorPolicy] = None
) -> Tuple[List[ActiveSignal], SuppressionReport]:
    """Apply a :class:`NoiseFloorPolicy` to folded signals (default policy if none).

    Convenience wrapper: ``visible, report = apply_noise_floors(active_signals)``.
    """
    return (policy or NoiseFloorPolicy()).apply(signals)
