"""MSP-B7 / AT-672 — tests for per-run event-volume budgets (loud degradation).

Covers the T4 acceptance criterion and its supporting guarantees:
  * AC4  — a run exceeding its event budget completes on the budgeted window and
           reports deferred volume in the run record / run-health surface —
           never silent truncation.
  * the budgeted window is exactly the first `limit` events (arrival order);
           deferred events are counted, not folded, and the run never crashes.
  * the report is JSON-serialisable and carries budget / processed / deferred /
           per-source breakdown / deferred window / reason.
  * no budget (default) processes everything; budget honoured across sources.

DB-free.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from discovery.signals import (
    OperationalEvent,
    OpsEventStream,
    ResourceRef,
    RunBudget,
)

_DAY = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


def _event(n, *, org_id="acme", source_system="aws_cloudtrail", resource_id=None):
    """A distinct event (unique signature per n via a unique event_type/resource)."""
    return OperationalEvent.build(
        org_id=org_id,
        source_system=source_system,
        signal_id=f"evt-{n}",
        event_type=f"Action-{n}",
        event_class="lifecycle",
        severity="info",
        resource=ResourceRef(provider="aws", resource_type="compute",
                             resource_id=resource_id or f"i-{n}"),
        observed_at=(_DAY + timedelta(seconds=n)).isoformat(),
    )


# ── AC4: run exceeding budget completes on budgeted window & reports deferral ─

def test_ac4_run_over_budget_completes_and_reports_deferred_volume():
    stream = OpsEventStream(budget=100)
    dispositions = [stream.admit(_event(i)) for i in range(250)]

    # the run completed (no crash) and processed exactly the budgeted window.
    processed = [d for d in dispositions if not d.is_deferred]
    deferred = [d for d in dispositions if d.is_deferred]
    assert len(processed) == 100
    assert len(deferred) == 150

    # only budgeted-window events became detector-visible signals.
    assert len(stream.active_signals()) == 100

    # the deferred volume is reported — visible, never silent truncation.
    report = stream.budget_report()
    assert report.budget == 100
    assert report.processed == 100
    assert report.deferred == 150
    assert report.seen == 250
    assert report.breached is True
    assert "150" in report.reason and "budget of 100" in report.reason


def test_ac4_report_is_json_serialisable_run_shape():
    import json

    stream = OpsEventStream(budget=10)
    for i in range(25):
        stream.admit(_event(i))
    d = stream.budget_report().to_dict()
    json.dumps(d)  # must not raise
    assert d["budget"] == 10
    assert d["processed"] == 10
    assert d["deferred"] == 15
    assert d["seen"] == 25
    assert d["breached"] is True
    assert d["reason"] is not None
    # deferred window spans the deferred events (events 10..24 here).
    assert d["deferred_window"]["first"] == _event(10).observed_at
    assert d["deferred_window"]["last"] == _event(24).observed_at


def test_ac4_budgeted_window_is_first_events_in_arrival_order():
    stream = OpsEventStream(budget=3)
    admitted = [stream.admit(_event(i)) for i in range(6)]
    # first 3 processed, last 3 deferred (arrival-ordered window).
    assert [not a.is_deferred for a in admitted] == [True, True, True, False, False, False]
    kept = {s.provider_event_ids[0] for s in stream.active_signals()}
    assert kept == {"evt-0", "evt-1", "evt-2"}


def test_ac4_deferred_events_are_not_folded_never_silently_dropped():
    stream = OpsEventStream(budget=2)
    for i in range(10):
        adm = stream.admit(_event(i))
        if i >= 2:
            assert adm.is_deferred
            assert adm.signal is None
    # every one of the 10 events is accounted for (2 processed + 8 deferred).
    r = stream.budget_report()
    assert r.processed + r.deferred == 10
    assert r.deferred == 8


# ── per-source breakdown ─────────────────────────────────────────────────────

def test_deferred_by_source_breakdown():
    stream = OpsEventStream(budget=1)
    stream.admit(_event(0, source_system="aws_cloudtrail"))   # processed
    stream.admit(_event(1, source_system="aws_cloudtrail"))   # deferred
    stream.admit(_event(2, source_system="azure_monitor"))    # deferred
    stream.admit(_event(3, source_system="azure_monitor"))    # deferred
    report = stream.budget_report()
    assert report.deferred == 3
    assert report.deferred_by_source == {"aws_cloudtrail": 1, "azure_monitor": 2}


# ── no budget → everything processed ────────────────────────────────────────

def test_no_budget_processes_everything():
    stream = OpsEventStream()  # budget None
    for i in range(500):
        assert not stream.admit(_event(i)).is_deferred
    report = stream.budget_report()
    assert report.budget is None
    assert report.deferred == 0
    assert report.breached is False
    assert report.reason is None
    assert report.deferred_window is None


def test_budget_exactly_at_volume_never_breaches():
    stream = OpsEventStream(budget=50)
    for i in range(50):
        stream.admit(_event(i))
    r = stream.budget_report()
    assert r.processed == 50 and r.deferred == 0 and r.breached is False


# ── re-fires consume budget (volume-based, not distinct-signal-based) ────────

def test_refires_count_toward_budget_volume():
    # 10 firings of ONE stuck alarm, budget 4 → 4 processed (fold to 1 signal,
    # count 4), 6 deferred. The budget is about event VOLUME, not signal count.
    stream = OpsEventStream(budget=4)
    ref = ResourceRef(provider="aws", resource_type="compute", resource_id="i-stuck")
    for i in range(10):
        stream.admit(OperationalEvent.build(
            org_id="acme", source_system="aws_cloudwatch", signal_id=f"f-{i}",
            event_type="ALARM State Change", event_class="state_change", severity="high",
            resource=ref, observed_at=(_DAY + timedelta(minutes=i)).isoformat(),
        ))
    signals = stream.active_signals()
    assert len(signals) == 1
    assert signals[0].occurrence_count == 4     # only the budgeted window folded
    r = stream.budget_report()
    assert r.processed == 4 and r.deferred == 6


# ── RunBudget unit + validation ──────────────────────────────────────────────

def test_run_budget_capacity_and_charge():
    b = RunBudget(2)
    assert b.has_capacity()
    b.charge()
    assert b.has_capacity()
    b.charge()
    assert not b.has_capacity()   # 2 processed, budget 2 → full


def test_zero_budget_defers_everything():
    stream = OpsEventStream(budget=0)
    assert stream.admit(_event(0)).is_deferred
    assert stream.active_signals() == []
    assert stream.budget_report().deferred == 1


def test_negative_budget_rejected():
    with pytest.raises(ValueError):
        OpsEventStream(budget=-1)
    with pytest.raises(ValueError):
        RunBudget(-5)
