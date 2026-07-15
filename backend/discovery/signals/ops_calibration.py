"""MSP-B7 / AT-674 (T6) — event-volume calibration from B8's month-scale sample.

The five disciplines (T1–T5) each ship a *default* — noise floors, the per-run
event budget, correlation windows. T6 is the discipline that makes those defaults
**evidence-based, not guessed**: it derives them from MSP-B8's measured
month-scale volume run and documents each with its rationale. This module is the
single, auditable source of truth for those calibrated defaults; the T3/T4/T5
modules import them from here rather than hardcoding a number.

The measured input (MSP-B8)
---------------------------
B8 ran a representative month of AWS+Azure exports end-to-end and recorded the
numbers in ``docs/MSP-B8_VOLUME_VALIDATION.md`` (its T5 / AC7 output). The
load-bearing figures this calibration consumes are captured verbatim in
:data:`B8_MEASUREMENTS` — measured, reproducible (deterministic corpus), and
pointer-linked to the doc so a reviewer can trace every derived default back to a
real measurement. Calibration reruns whenever B8 re-measures: change the numbers
here, and the derived defaults move with them.

What B8 measured — and what it did not
--------------------------------------
B8 measured **aggregate volume, throughput, and memory** for a month: ~30,225
events, ~678 events/s ingest (~1.47 ms/event, tracemalloc active → conservative),
89.6 MB flat peak memory. It did **not** publish a per-event-class recurrence
histogram or an event↔incident lag distribution. So this calibration derives the
**budget** directly and quantitatively from the measured monthly volume, and sets
the **floors** and **windows** to operationally-justified defaults consistent with
the measured event density — documenting, honestly, exactly which defaults are
volume-derived and which await finer per-class / lag telemetry (the calibration is
evidence-based and says where the evidence stops).
"""

from __future__ import annotations

from typing import Any, Dict

# ─────────────────────────────────────────────────────────────────────────────
# The measured input — MSP-B8 month-scale validation (docs/MSP-B8_VOLUME_VALIDATION.md)
# ─────────────────────────────────────────────────────────────────────────────

#: Verbatim load-bearing figures from B8's recorded month-scale run. Measured, not
#: assumed. Source of truth for every derived default below.
B8_MEASUREMENTS: Dict[str, Any] = {
    "source": "docs/MSP-B8_VOLUME_VALIDATION.md",
    "org": "org_volume_ac7",
    "month_events_generated": 30_225,     # representative month of AWS+Azure exports
    "month_events_ingested": 29_553,      # normalized events emitted (post skip+dedupe)
    "malformed_skipped": 447,
    "duplicates_collapsed": 225,
    "ingest_events_per_sec": 678.5,       # measured, tracemalloc active (conservative)
    "per_event_ingest_ms": 1.474,         # measured per-event ingest cost (conservative)
    "peak_memory_mb": 89.61,              # flat — volume drives time, not memory
    "bridge_batch_size": 500,
}


# ─────────────────────────────────────────────────────────────────────────────
# Derived: per-run event-volume BUDGET (T4)
# ─────────────────────────────────────────────────────────────────────────────
# Quantitatively derived from the measured monthly volume. The budget must never
# clip a normal month, must tolerate a substantial spike or a multi-month
# catch-up/backfill, and must still bound per-run cost. We take a headroom
# multiple of the measured month and round to a clean operator-facing number.

#: Measured events in a representative month (the volume the budget is sized against).
MEASURED_MONTHLY_EVENT_VOLUME = B8_MEASUREMENTS["month_events_generated"]  # 30,225

#: Headroom over one measured month. ×8 tolerates an 8×-noisier month or an
#: ~8-month backfill in a single run while keeping the worst-case ingest bounded.
RUN_BUDGET_HEADROOM_FACTOR = 8

#: Calibrated per-run event budget: ceil(8 × 30,225) = 241,800, rounded up to a
#: clean 250,000. At the measured ~1.474 ms/event that is a ~6-minute worst-case
#: ingest ceiling, and B8's flat ~89.6 MB peak means volume drives time, not
#: memory — so the ceiling is about run duration/cost, exactly what a per-run
#: budget should bound. `OpsEventStream` stays opt-in (`budget=None`); production
#: wiring passes this value.
CALIBRATED_RUN_EVENT_BUDGET = 250_000


# ─────────────────────────────────────────────────────────────────────────────
# Derived: noise FLOORS per event class (T3)
# ─────────────────────────────────────────────────────────────────────────────
# B8 measured ~30,225 events/month across four surfaces (~7,575 each) ≈ ~1,000
# events/day. It did NOT publish a per-class recurrence histogram, so a precise
# per-class floor cannot be *quantitatively* derived from B8 yet. We therefore set
# CONSERVATIVE floors for the demonstrably-noisy, high-cardinality classes (audit
# floods, state-change storms, access chatter) and floor everything else at 1
# (never suppressed) — critically, `error` and `security` are never floored, so a
# security finding is never silently dropped. A floor of 5 within a one-day active
# period means "surface only if it recurs ≥5×/day": at ~1,000 events/day this
# filters one-off/low-count noise while keeping any genuinely recurring signal.
# These are refined to exact per-class values once per-class recurrence telemetry
# is captured (an explicit follow-on to B8's aggregate numbers).

#: Calibrated per-event-class noise floors (minimum daily recurrence to be visible).
CALIBRATED_NOISE_FLOORS: Dict[str, int] = {
    "audit": 5,          # audit floods
    "state_change": 5,   # state-change storms
    "access": 5,         # access / API chatter
}

#: Floor for any class not named above — 1 = never suppressed (incl. error/security).
CALIBRATED_DEFAULT_FLOOR = 1


# ─────────────────────────────────────────────────────────────────────────────
# Derived: correlation WINDOWS per join type (T5)
# ─────────────────────────────────────────────────────────────────────────────
# B8's measured density (~30,225 events/month ≈ ~1,000/day ≈ ~42/hour across
# surfaces) is the evidence for the event↔event window: at ~42 events/hour a
# 15-minute cross-provider window already admits ~10 unrelated events, so the
# window must stay TIGHT to avoid coincidence joins — 15 min is the calibrated
# ceiling, not a loose guess. B8 did not measure event↔incident lag; 2 hours is
# the operationally-justified default (incident creation commonly lags the
# triggering event by up to hours) and is per-org tunable. Anything else falls
# back to a 1-hour window.

#: Calibrated per-join-type correlation windows, in SECONDS.
CALIBRATED_CORRELATION_WINDOWS: Dict[str, int] = {
    "event_incident": 2 * 3600,   # 2h  — operational incident-creation lag
    "event_event": 15 * 60,       # 15m — kept tight vs measured ~42 events/hour
}

#: Fallback window (seconds) for a join type with no calibrated default.
CALIBRATED_DEFAULT_WINDOW_SECONDS = 3600   # 1h


# ─────────────────────────────────────────────────────────────────────────────
# Summary (run-health / telemetry / audit)
# ─────────────────────────────────────────────────────────────────────────────

def calibration_summary() -> Dict[str, Any]:
    """A JSON-serialisable snapshot of the calibration and its derivation.

    Exposes the measured B8 input, the derived defaults, and the rationale keys so
    a run-health surface or an auditor can see that the MSP-B7 volume defaults are
    evidence-based (sourced from :data:`B8_MEASUREMENTS`), not guessed (AC7).
    """
    return {
        "measured_input": dict(B8_MEASUREMENTS),
        "budget": {
            "calibrated_run_event_budget": CALIBRATED_RUN_EVENT_BUDGET,
            "measured_monthly_event_volume": MEASURED_MONTHLY_EVENT_VOLUME,
            "headroom_factor": RUN_BUDGET_HEADROOM_FACTOR,
            "derivation": (
                f"ceil({RUN_BUDGET_HEADROOM_FACTOR} × {MEASURED_MONTHLY_EVENT_VOLUME}) "
                f"rounded up to {CALIBRATED_RUN_EVENT_BUDGET}"
            ),
        },
        "noise_floors": {
            "floors": dict(CALIBRATED_NOISE_FLOORS),
            "default_floor": CALIBRATED_DEFAULT_FLOOR,
            "basis": "conservative floors for high-cardinality classes; error/security never floored",
        },
        "correlation_windows": {
            "windows_seconds": dict(CALIBRATED_CORRELATION_WINDOWS),
            "default_window_seconds": CALIBRATED_DEFAULT_WINDOW_SECONDS,
            "basis": "event↔event kept tight vs measured event density; event↔incident = operational lag",
        },
    }
