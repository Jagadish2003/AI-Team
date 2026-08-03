"""MSP-B7 / AT-672 — per-run event-volume budgets (loud degradation).

Dedup (T1), aggregation (T2), and noise floors (T3) shrink volume where they
safely can. This is the **fourth discipline** — the *per-run budget* — the hard
backstop that keeps a single run's cost bounded when a month of cloud events
would otherwise make it slow and expensive. A run has an event-volume budget; it
processes the **budgeted window** (the first ``limit`` events, in admission
order) and, on breach, **defers the rest** — recording exactly how much was
deferred, from which sources, and over what window.

Never silent truncation
------------------------
The whole point is honesty at scale: a budget breach is *an operator decision
surfaced, not a data loss hidden*. The :class:`BudgetReport` this module produces
carries the budget, the processed and deferred volumes, the per-source deferred
breakdown, the deferred time window, and a human-readable reason — ready to be
written into the run record and the R18-C2 run-health content panel. If nothing
was deferred, the report says so; if 40 000 events were deferred, the report says
that, loudly. A run always *completes* — it never crashes and never quietly drops
events off the end.

Where it sits in admission
--------------------------
Per the MSP-B7 pipeline (dedup → floor → budget → aggregate), the budget is
enforced **during admission** — it must be, because its job is to stop the run
from *processing* everything (post-hoc filtering would already have paid the
cost). :class:`RunBudget` is the counter :class:`~discovery.signals.ops_stream.OpsEventStream`
consults per event: while it ``has_capacity`` the event is folded and charged;
once the budget is exhausted every further event is deferred-and-counted. Because
the budgeted window is the first ``limit`` events in arrival order, budget
enforcement is deliberately arrival-ordered (unlike the order-independent
dedup/floor/aggregate stages) — a budget is a statement about processing order.

Scope (T4 only)
---------------
The budget limit is configurable per run; its calibrated default comes from B8's
month-scale measurements in T6. Correlation windows (T5) are a separate task.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Optional

from .ops_calibration import CALIBRATED_RUN_EVENT_BUDGET

#: Recommended per-run event-volume budget — CALIBRATED from B8's month-scale
#: sample (MSP-B7 T6, ≈8× a measured month; see
#: :mod:`discovery.signals.ops_calibration`). ``OpsEventStream`` stays opt-in
#: (``budget=None`` → unbounded); production wiring passes this value.
DEFAULT_RUN_EVENT_BUDGET = CALIBRATED_RUN_EVENT_BUDGET


@dataclass
class BudgetReport:
    """Immutable snapshot of a run's event-budget outcome — the loud-degradation proof.

    Written into the run record and surfaced in the R18-C2 run-health content
    panel. ``breached`` is true iff any event was deferred; ``deferred_by_source``
    and ``deferred_window`` say exactly what was held back and when, so a
    reviewer can see the truncation decision rather than infer a silent one.
    """

    budget: Optional[int]
    processed: int
    deferred: int
    deferred_by_source: Dict[str, int] = field(default_factory=dict)
    deferred_window: Optional[Dict[str, str]] = None
    #: How many of ``processed`` were at-least-once REDELIVERIES of a firing the
    #: run had already counted. They are charged (they cost the same fetch+map
    #: work, and the budget is what stops a redelivery storm paging for ever), so
    #: reporting them separately is what makes a budget depleted by provider churn
    #: distinguishable from one depleted by genuine event volume.
    duplicates: int = 0

    @property
    def seen(self) -> int:
        """Total events the run saw (processed + deferred)."""
        return self.processed + self.deferred

    @property
    def breached(self) -> bool:
        """True iff the budget was exceeded and volume was deferred."""
        return self.deferred > 0

    @property
    def reason(self) -> Optional[str]:
        """Human-readable explanation of the degradation (None when not breached)."""
        if not self.breached:
            return None
        return (
            f"event budget of {self.budget} reached; "
            f"{self.deferred} event(s) deferred to a future run"
        )

    def to_dict(self) -> Dict[str, Any]:
        """The per-run report shape (run record + R18-C2 content panel)."""
        return {
            "budget": self.budget,
            "processed": self.processed,
            "duplicates": self.duplicates,
            "deferred": self.deferred,
            "seen": self.seen,
            "breached": self.breached,
            "deferred_by_source": dict(sorted(self.deferred_by_source.items())),
            "deferred_window": self.deferred_window,
            "reason": self.reason,
        }


class RunBudget:
    """Mutable per-run event-volume counter enforced during admission.

    ``limit`` is the maximum number of events the run will process; ``None`` means
    unbounded (no budget). The owning stream calls :meth:`has_capacity` before
    processing an event and :meth:`charge` when it does; events that arrive with
    no capacity left are handed to :meth:`defer`, which tallies them and tracks
    the deferred time window. Timestamps are supplied pre-parsed by the stream
    (this module never parses) so it stays dependency-free.
    """

    def __init__(self, limit: Optional[int] = None):
        if limit is not None and limit < 0:
            raise ValueError("budget limit must be >= 0 or None")
        self.limit = limit
        self.processed = 0
        self.duplicates = 0
        self.deferred = 0
        self._deferred_by_source: Dict[str, int] = {}
        self._first_deferred_dt: Optional[datetime] = None
        self._last_deferred_dt: Optional[datetime] = None
        self._first_deferred_ts: Optional[str] = None
        self._last_deferred_ts: Optional[str] = None

    def has_capacity(self) -> bool:
        """True while the run may still process another event within budget."""
        return self.limit is None or self.processed < self.limit

    def charge(self, *, duplicate: bool = False) -> None:
        """Count one processed (admitted) event against the budget.

        ``duplicate`` marks an at-least-once redelivery of an already-counted
        firing. It is still charged — it cost the same fetch and mapping work, and
        the budget is what stops a redelivery storm paging for ever — but it is
        tallied separately so the report can say WHY a budget was depleted.
        """
        self.processed += 1
        if duplicate:
            self.duplicates += 1

    def defer(
        self, source_system: str, observed_at: Optional[str], observed_dt: Optional[datetime]
    ) -> None:
        """Tally one deferred event and extend the deferred time window.

        ``observed_dt`` is the pre-parsed observation time (``None`` if it could
        not be parsed — the event still counts, it just does not move the window).
        """
        self.deferred += 1
        src = source_system or "unknown"
        self._deferred_by_source[src] = self._deferred_by_source.get(src, 0) + 1
        if observed_dt is not None:
            if self._first_deferred_dt is None or observed_dt < self._first_deferred_dt:
                self._first_deferred_dt, self._first_deferred_ts = observed_dt, observed_at
            if self._last_deferred_dt is None or observed_dt > self._last_deferred_dt:
                self._last_deferred_dt, self._last_deferred_ts = observed_dt, observed_at

    def snapshot(self) -> BudgetReport:
        """Build the immutable :class:`BudgetReport` for the run record."""
        window = None
        if self._first_deferred_ts is not None or self._last_deferred_ts is not None:
            window = {"first": self._first_deferred_ts, "last": self._last_deferred_ts}
        return BudgetReport(
            budget=self.limit,
            processed=self.processed,
            duplicates=self.duplicates,
            deferred=self.deferred,
            deferred_by_source=dict(self._deferred_by_source),
            deferred_window=window,
        )
