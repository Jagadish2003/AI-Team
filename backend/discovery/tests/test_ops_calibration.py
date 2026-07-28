"""MSP-B7 / AT-674 (T6) — tests for the event-volume calibration pass.

Covers AC7: floors, budgets, and window defaults are set from B8's measured
month-scale sample and documented with their rationale — evidence-based, not
guessed. Verifies:
  * the calibration is sourced from B8's recorded month-scale measurements
    (docs/MSP-B8_VOLUME_VALIDATION.md), traceable via B8_MEASUREMENTS;
  * the per-run budget is quantitatively derived from the measured monthly volume
    (with headroom) and comfortably clears a real month;
  * the T3 floors, T4 budget, and T5 windows modules all source their defaults
    from the single calibration module (no divergent hardcoded guesses);
  * the calibration summary is JSON-serialisable for run-health/audit.

DB-free.
"""
from __future__ import annotations

import json
import os

from discovery.signals import (
    B8_MEASUREMENTS,
    CALIBRATED_CORRELATION_WINDOWS,
    CALIBRATED_DEFAULT_FLOOR,
    CALIBRATED_DEFAULT_WINDOW_SECONDS,
    CALIBRATED_NOISE_FLOORS,
    CALIBRATED_RUN_EVENT_BUDGET,
    DEFAULT_RUN_EVENT_BUDGET,
    calibration_summary,
)
from discovery.signals.ops_calibration import (
    MEASURED_MONTHLY_EVENT_VOLUME,
    RUN_BUDGET_HEADROOM_FACTOR,
)


# ── the measured input is B8's recorded month-scale sample ──────────────────

def test_calibration_sourced_from_b8_measurements():
    # The input is the actual B8 doc, not an assumption.
    assert B8_MEASUREMENTS["source"] == "docs/MSP-B8_VOLUME_VALIDATION.md"
    # load-bearing measured figures are present and positive.
    assert B8_MEASUREMENTS["month_events_generated"] == 30_225
    assert B8_MEASUREMENTS["month_events_ingested"] == 29_553
    assert B8_MEASUREMENTS["per_event_ingest_ms"] > 0
    assert B8_MEASUREMENTS["peak_memory_mb"] > 0


def test_b8_source_document_exists():
    # the cited calibration input actually exists in the repo.
    here = os.path.dirname(__file__)
    doc = os.path.abspath(os.path.join(here, "..", "..", "..", B8_MEASUREMENTS["source"]))
    assert os.path.isfile(doc), f"missing B8 calibration input: {doc}"


# ── budget: quantitatively derived from measured monthly volume ─────────────

def test_budget_derived_from_measured_monthly_volume():
    # budget = headroom × measured month, rounded up to a clean number.
    raw = RUN_BUDGET_HEADROOM_FACTOR * MEASURED_MONTHLY_EVENT_VOLUME
    assert CALIBRATED_RUN_EVENT_BUDGET >= raw          # rounded UP, never below
    assert CALIBRATED_RUN_EVENT_BUDGET == 250_000
    assert MEASURED_MONTHLY_EVENT_VOLUME == B8_MEASUREMENTS["month_events_generated"]


def test_budget_never_clips_a_normal_month():
    # a normal measured month must fit comfortably inside the budget.
    assert CALIBRATED_RUN_EVENT_BUDGET > B8_MEASUREMENTS["month_events_generated"]
    # with meaningful headroom (≥ several months of catch-up).
    assert CALIBRATED_RUN_EVENT_BUDGET >= 5 * B8_MEASUREMENTS["month_events_generated"]


def test_default_run_event_budget_is_the_calibrated_value():
    assert DEFAULT_RUN_EVENT_BUDGET == CALIBRATED_RUN_EVENT_BUDGET


# ── floors: calibration is the single source of truth for T3 defaults ───────

def test_noise_floor_module_uses_calibrated_defaults():
    from discovery.signals import noise_floor

    assert noise_floor.DEFAULT_NOISE_FLOORS == CALIBRATED_NOISE_FLOORS
    assert noise_floor.DEFAULT_FLOOR == CALIBRATED_DEFAULT_FLOOR


def test_error_and_security_never_floored_by_calibration():
    # calibration must never suppress error/security (never a silent drop).
    assert "error" not in CALIBRATED_NOISE_FLOORS
    assert "security" not in CALIBRATED_NOISE_FLOORS
    assert CALIBRATED_DEFAULT_FLOOR == 1


# ── windows: calibration is the single source of truth for T5 defaults ──────

def test_correlation_windows_module_uses_calibrated_defaults():
    from discovery.correlation import windows

    assert windows.DEFAULT_CORRELATION_WINDOWS == CALIBRATED_CORRELATION_WINDOWS
    assert windows.DEFAULT_WINDOW_SECONDS == CALIBRATED_DEFAULT_WINDOW_SECONDS


def test_event_event_window_kept_tight_vs_measured_density():
    # measured density ≈ ~42 events/hour; event↔event window stays well under an
    # hour so a cross-provider join cannot sweep in a large coincidence set.
    assert CALIBRATED_CORRELATION_WINDOWS["event_event"] <= 15 * 60
    assert CALIBRATED_CORRELATION_WINDOWS["event_incident"] == 2 * 3600


# ── the summary is auditable & serialisable ─────────────────────────────────

def test_calibration_summary_is_json_serialisable_and_traceable():
    summary = calibration_summary()
    json.dumps(summary)  # must not raise
    # traces every derived family back to the measured input.
    assert summary["measured_input"]["source"] == "docs/MSP-B8_VOLUME_VALIDATION.md"
    assert summary["budget"]["calibrated_run_event_budget"] == CALIBRATED_RUN_EVENT_BUDGET
    assert "derivation" in summary["budget"]
    assert summary["noise_floors"]["floors"] == CALIBRATED_NOISE_FLOORS
    assert summary["correlation_windows"]["windows_seconds"] == CALIBRATED_CORRELATION_WINDOWS
