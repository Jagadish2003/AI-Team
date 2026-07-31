"""Regression tests for the PR-review findings on #488 and #505.

Each test below is named for the defect it pins, and every test covering a REAL
defect was verified to fail against the code as it stood before the accompanying
fix — a guard nobody has seen go red is not known to be a guard. The exceptions are
labelled in place: the control tests that pin unchanged good behaviour, and
``test_row_id_zero_is_a_position_not_an_absent_id``, whose finding turned out not to
be reachable.

The two serious ones are both in the descending CloudTrail backfill and both end in
permanent data loss, which is exactly the failure MSP-B1's posture section forbids:

  * a truncated page that reads nothing was treated as a DRAINED window, promoting
    the pending high-water mark and stranding the whole unread backlog below it;
  * a same-instant ceiling was treated as "no progress" even when the poll had read
    new ids at that instant, pinning a walk that could in fact continue.

The rest pin smaller things: the staging cursor's falsy-``row_id`` trap, the budget
bound's silent-failure path, and the two behaviours the review proposed changing
that we deliberately kept (total-poll cap semantics, redeliveries charged).
"""
from __future__ import annotations

import logging

import pytest

from discovery.ingest.aws_watermark import (
    TimePosition,
    advance_descending,
)
from discovery.ingest.ops_event_bridge import OpsEventBridgeIngestor
from discovery.signals.budget import RunBudget

_T = "2026-07-14T03:00:00Z"


# ═════════════════════════════════════════════════════════════════════════════
# #505 — a truncated read that returns nothing is NOT a drained backfill
# ═════════════════════════════════════════════════════════════════════════════

def test_empty_truncated_page_pauses_the_backfill_instead_of_closing_it():
    """The must-fix: an unreadable page must not promote the pending high mark.

    CloudTrail's delivery is eventually consistent and its management-event filter
    drops records, so a page of a truncated read can legitimately contain nothing
    this position accepts. Closing the window there advances the watermark past a
    backlog that was never read, and the next run resumes incrementally ABOVE it —
    those events can never be reached again.
    """
    mid_backfill = TimePosition(
        watermark="2026-07-01T00:00:00Z",
        ceiling="2026-07-10T00:00:00Z",
        ceiling_ids=("old-1",),
        pending_high="2026-07-14T03:00:00Z",
        pending_high_ids=("new-1",),
    )

    result = advance_descending(mid_backfill, [], truncated=True)

    assert result == mid_backfill, "the backfill window was closed by an empty page"
    assert result.backfilling, "the ceiling was cleared — the remaining backlog is stranded"
    assert result.watermark == "2026-07-01T00:00:00Z", (
        "pending_high was promoted, so the next run polls incrementally above the "
        "unread backlog and those events are lost for good"
    )


def test_empty_truncated_page_outside_a_backfill_still_holds_the_watermark():
    """The same rule on the first page: nothing read means nothing to advance to."""
    start = TimePosition(watermark="2026-07-01T00:00:00Z", boundary_ids=("a",))

    result = advance_descending(start, [], truncated=True)

    assert result.watermark == "2026-07-01T00:00:00Z"
    assert not result.backfilling


def test_a_genuinely_drained_window_still_promotes_the_pending_high_mark():
    """The fix must not stall an ordinary, correctly-completing backfill."""
    mid_backfill = TimePosition(
        watermark="2026-07-01T00:00:00Z",
        ceiling="2026-07-10T00:00:00Z",
        pending_high="2026-07-14T03:00:00Z",
        pending_high_ids=("new-1",),
    )

    result = advance_descending(
        mid_backfill, [("2026-07-09T00:00:00Z", "e-1")], truncated=False
    )

    assert result.watermark == "2026-07-14T03:00:00Z"
    assert not result.backfilling, "the window should be clear once the walk completes"


# ═════════════════════════════════════════════════════════════════════════════
# #505 — a same-instant ceiling can still be progress
# ═════════════════════════════════════════════════════════════════════════════

def test_same_instant_ceiling_carries_new_ids_forward_rather_than_stalling():
    """CloudTrail has second granularity, so one dense second can span polls.

    Reading new ids AT the ceiling instant IS progress: carrying them forward lets
    the next poll exclude them and reach the older remainder. Pinning instead left
    the walk re-reading the same second on every run — a permanent stall, not the
    self-resolving pause the pin was meant to be.
    """
    previous = TimePosition(
        watermark="2026-07-01T00:00:00Z",
        ceiling=_T,
        ceiling_ids=("seen-1",),
        pending_high="2026-07-14T09:00:00Z",
    )

    result = advance_descending(previous, [(_T, "fresh-1")], truncated=True)

    assert result.ceiling == _T
    assert result.ceiling_ids == ("seen-1", "fresh-1"), "newly-read ids were dropped"
    assert result.pending_high == "2026-07-14T09:00:00Z", "the high mark must survive"
    assert result.watermark == "2026-07-01T00:00:00Z", "the watermark must stay pinned"


def test_same_instant_ceiling_with_nothing_new_pins_and_says_so(caplog):
    """When the instant yields nothing new the walk genuinely cannot advance."""
    previous = TimePosition(
        watermark="2026-07-01T00:00:00Z",
        ceiling=_T,
        ceiling_ids=("seen-1",),
        pending_high="2026-07-14T09:00:00Z",
    )

    with caplog.at_level(logging.WARNING):
        result = advance_descending(previous, [(_T, "seen-1")], truncated=True)

    assert result == previous
    assert "no progress" in caplog.text, "a stalled walk must be loud"


def test_the_walk_terminates_because_carried_ids_are_bounded():
    """Carrying ids forward must not let a dense second page for ever.

    Ids are capped, so a pathological same-second burst converges on a pinned
    position instead of spinning — the cap can only cause a bounded re-delivery,
    which B7 admission folds.
    """
    from discovery.ingest.aws_watermark import MAX_BOUNDARY_IDS

    position = TimePosition(watermark="2026-07-01T00:00:00Z", ceiling=_T, pending_high=_T)
    events = [(_T, f"e-{i}") for i in range(MAX_BOUNDARY_IDS * 2)]

    first = advance_descending(position, events, truncated=True)
    assert len(first.ceiling_ids) == MAX_BOUNDARY_IDS

    # Re-offering the same instant now adds nothing, so the walk pins rather than
    # looping — the terminating case.
    assert advance_descending(first, events, truncated=True) == first


# ═════════════════════════════════════════════════════════════════════════════
# #488 — staging cursor must not treat row_id 0 as "no id"
# ═════════════════════════════════════════════════════════════════════════════

class _Row:
    """Minimal staging row — only what the cursor logic reads."""

    def __init__(self, row_id):
        self.row_id = row_id
        self.provider = "aws"
        self.source_format = "cloudwatch"
        self.provider_event_id = f"pe-{row_id}"


class _OneShotReader:
    """Returns one page of rows the first time, then nothing."""

    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def fetch_after(self, org_id, *, after_row_id, limit):
        self.calls.append(after_row_id)
        if len(self.calls) > 1:
            return []
        return list(self.rows)


def test_row_id_zero_is_a_position_not_an_absent_id():
    """The falsy-``row_id`` finding is DEFENSIVE, not a live bug — recorded as such.

    ``after = row.row_id or after`` reads row_id 0 as "no id". It cannot currently
    bite: the cursor floors at 0 and the reader only ever returns rows with
    ``row_id > after``, so row_id 0 reaches this loop only when ``after`` is already
    0 and the two expressions agree. This test therefore does NOT go red against the
    old code, and is here to pin the invariant rather than to claim a fixed defect —
    the cursor must take the row's own position for every value the column's type
    allows, so a future reader or a re-based identity column cannot resurrect it.
    """
    reader = _OneShotReader([_Row(0)])
    ingestor = OpsEventBridgeIngestor(reader=reader, batch_size=10)

    batches = list(ingestor.ingest_changes("org-1", None))

    assert batches[-1].next_checkpoint == "0"


def test_ordinary_row_ids_still_advance_the_checkpoint():
    reader = _OneShotReader([_Row(7), _Row(8)])
    ingestor = OpsEventBridgeIngestor(reader=reader, batch_size=10)

    batches = list(ingestor.ingest_changes("org-1", None))

    assert batches[-1].next_checkpoint == "8"


# ═════════════════════════════════════════════════════════════════════════════
# #488 — redeliveries are charged (kept), but now explainable
# ═════════════════════════════════════════════════════════════════════════════

def test_redeliveries_are_charged_and_reported_separately():
    """Kept deliberately: the budget bounds WORK, not distinct facts.

    The poll loop stops fetching on ``has_capacity()``. If a redelivery were free, a
    provider redelivery storm would page for ever doing real fetch+map work with the
    budget never moving — the hang class the poll bounds exist to prevent. Charging
    it is right; making it invisible was not, so the report now says how much of a
    depleted budget went on churn.
    """
    budget = RunBudget(limit=10)
    budget.charge()
    budget.charge(duplicate=True)
    budget.charge(duplicate=True)

    report = budget.snapshot()

    assert report.processed == 3, "a redelivery still costs the run a fetch and a map"
    assert report.duplicates == 2
    assert report.to_dict()["duplicates"] == 2, (
        "the run record must be able to distinguish a budget spent on provider churn "
        "from one spent on genuine event volume"
    )


def test_duplicates_default_to_zero_for_a_clean_run():
    budget = RunBudget(limit=5)
    budget.charge()

    assert budget.snapshot().duplicates == 0
    assert budget.snapshot().to_dict()["duplicates"] == 0


# ═════════════════════════════════════════════════════════════════════════════
# #505 — the poll cap counts TOTAL polls (kept; the docs were what was wrong)
# ═════════════════════════════════════════════════════════════════════════════

def test_poll_cap_counts_total_polls_including_the_first():
    """Pins the semantic the review proposed inverting.

    ``max_polls_per_scope`` bounds total polls of a scope, first included, so ``1``
    means "poll once, never continue". Counting continuations instead would make
    that case inexpressible — ``0`` is already taken by unbounded.
    """
    from discovery.ingest.cloud_event_connector import STOP_POLL_CAP
    from discovery.ingest.aws_event_connector import AWSEventConnector

    connector = AWSEventConnector(
        _NullSource(), max_polls_per_scope=1, poll_deadline_seconds=0
    )

    assert connector._continuation_stop_reason(1, 0.0) == STOP_POLL_CAP, (
        "a cap of 1 must permit exactly one poll and no continuation"
    )

    connector = AWSEventConnector(
        _NullSource(), max_polls_per_scope=4, poll_deadline_seconds=0
    )
    assert connector._continuation_stop_reason(3, 0.0) is None
    assert connector._continuation_stop_reason(4, 0.0) == STOP_POLL_CAP


def test_a_failing_budget_check_is_logged_not_swallowed(caplog):
    """The budget is one of only three bounds — losing it must be visible.

    With the poll cap and deadline both disabled it is the ONLY bound, so a silent
    failure here restores the unbounded hang the module exists to prevent.
    """
    from discovery.ingest.aws_event_connector import AWSEventConnector

    connector = AWSEventConnector(
        _NullSource(), max_polls_per_scope=0, poll_deadline_seconds=0
    )

    class _BrokenStream:
        def has_capacity(self):
            raise AttributeError("no read side")

    connector.stream = _BrokenStream()

    with caplog.at_level(logging.WARNING):
        assert connector._continuation_stop_reason(1, 0.0) is None

    assert "budget bound unavailable" in caplog.text, (
        "the poll loop lost its only remaining bound without saying so"
    )


class _NullSource:
    """A poll source that is never actually polled by these unit-level checks."""

    def list_scopes(self, org_id):
        return []

    def poll(self, org_id, scope, position):  # pragma: no cover - never reached
        raise AssertionError("not polled")
