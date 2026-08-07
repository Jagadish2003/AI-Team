"""2.0-A2 T3 — comparability assessment and movement arithmetic.

Pure unit tests. The comparability verdict is where this subtask's honesty lives:
a delta between a 30-day baseline window and a 9-day post-action window is not a
result, it is a category error — so the assessment must NAME the problem rather
than silently normalising it, and must never come back null.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from app.opportunity_movement_record import (
    BOUNDARY_NO_OBSERVATIONS,
    BOUNDARY_STRICTLY_AFTER,
    DIRECTION_IMPROVED,
    DIRECTION_UNCHANGED,
    DIRECTION_UNKNOWN,
    DIRECTION_WORSENED,
    MAX_RUN_GAP_DAYS,
    MIN_POST_ACTION_OBSERVATIONS,
    REASON_BOUNDARY_STRADDLE,
    REASON_CADENCE_GAP,
    REASON_HORIZON_NOT_ELAPSED,
    REASON_HORIZON_UNKNOWN,
    REASON_SEASONALITY_MISMATCH,
    REASON_SPARSE_SAMPLING,
    REASON_WINDOW_LENGTH_MISMATCH,
    REASON_WINDOW_UNKNOWN,
    REQUIRED_MOVEMENT_FIELDS,
    VERDICT_COMPARABLE,
    VERDICT_NOT_COMPARABLE,
    VERDICT_WEAK,
    assess_comparability,
    build_movement_record,
    build_signal_movements,
    missing_movement_fields,
)


def dt(y, m, d):
    return datetime(y, m, d, tzinfo=timezone.utc)


def clean_assessment(**overrides):
    """A fully comparable assessment; each test degrades exactly one axis."""
    kwargs = dict(
        baseline_window_days=90,
        current_window_days=90,
        # Same months of the year, one year apart — no seasonal mismatch.
        baseline_window_start=dt(2025, 4, 1),
        baseline_window_end=dt(2025, 6, 30),
        current_window_start=dt(2026, 4, 1),
        current_window_end=dt(2026, 6, 30),
        action_date=date(2026, 1, 1),
        measured_at=dt(2026, 6, 30),
        projected_horizon_days=30,
        post_action_observation_count=3,
        post_action_run_dates=[dt(2026, 4, 10), dt(2026, 5, 10), dt(2026, 6, 10)],
    )
    kwargs.update(overrides)
    return assess_comparability(**kwargs)


# --------------------------------------------------------------------------
# The verdict is always populated
# --------------------------------------------------------------------------


class TestVerdictIsAlwaysPopulated:
    def test_a_clean_comparison_is_comparable_with_no_reasons(self):
        assessment = clean_assessment()
        assert assessment.verdict == VERDICT_COMPARABLE
        assert assessment.reasons == []

    def test_even_the_worst_case_returns_a_verdict(self):
        """Never null — a null would be read as "fine" by anything rendering it."""
        assessment = assess_comparability(
            baseline_window_days=None,
            current_window_days=None,
            baseline_window_start=None,
            baseline_window_end=None,
            current_window_start=None,
            current_window_end=None,
            action_date=None,
            measured_at=dt(2026, 6, 30),
            projected_horizon_days=None,
            post_action_observation_count=0,
            post_action_run_dates=[],
        )
        assert assessment.verdict in (
            VERDICT_COMPARABLE,
            VERDICT_WEAK,
            VERDICT_NOT_COMPARABLE,
        )
        assert assessment.verdict != VERDICT_COMPARABLE
        assert assessment.reasons

    def test_the_assessment_is_json_shaped(self):
        payload = clean_assessment().to_dict()
        assert payload["verdict"]
        for key in (
            "reasons",
            "baselineWindowDays",
            "currentWindowDays",
            "elapsedDaysSinceAction",
            "projectedHorizonDays",
            "postActionObservationCount",
            "boundaryHandling",
        ):
            assert key in payload


# --------------------------------------------------------------------------
# 1. Unequal window lengths
# --------------------------------------------------------------------------


class TestUnequalWindowLengths:
    def test_a_wildly_shorter_window_is_not_comparable(self):
        """The subtask's own example: 30-day baseline vs 9-day post-action."""
        assessment = clean_assessment(baseline_window_days=30, current_window_days=9)
        assert assessment.verdict == VERDICT_NOT_COMPARABLE
        assert REASON_WINDOW_LENGTH_MISMATCH in assessment.reasons
        assert any("differ by" in n for n in assessment.notes)

    def test_a_moderately_different_window_is_weakly_comparable(self):
        assessment = clean_assessment(baseline_window_days=90, current_window_days=60)
        assert assessment.verdict == VERDICT_WEAK
        assert REASON_WINDOW_LENGTH_MISMATCH in assessment.reasons

    def test_a_small_difference_is_still_comparable(self):
        assessment = clean_assessment(baseline_window_days=90, current_window_days=85)
        assert assessment.verdict == VERDICT_COMPARABLE
        assert REASON_WINDOW_LENGTH_MISMATCH not in assessment.reasons

    def test_an_unknown_window_length_is_itself_a_problem(self):
        """Not knowing is stated, not assumed away."""
        assessment = clean_assessment(current_window_days=None)
        assert assessment.verdict == VERDICT_WEAK
        assert REASON_WINDOW_UNKNOWN in assessment.reasons

    def test_the_windows_are_never_silently_normalised(self):
        """Both lengths are carried so a reader can judge for themselves."""
        assessment = clean_assessment(baseline_window_days=90, current_window_days=30)
        assert assessment.baseline_window_days == 90
        assert assessment.current_window_days == 30


# --------------------------------------------------------------------------
# 2. Insufficient elapsed time
# --------------------------------------------------------------------------


class TestElapsedTime:
    def test_measuring_before_the_projected_horizon_is_not_comparable(self):
        assessment = clean_assessment(
            action_date=date(2026, 6, 20),
            measured_at=dt(2026, 6, 30),
            projected_horizon_days=90,
        )
        assert assessment.verdict == VERDICT_NOT_COMPARABLE
        assert REASON_HORIZON_NOT_ELAPSED in assessment.reasons
        assert assessment.elapsed_days_since_action == 10

    def test_measuring_after_the_horizon_is_fine(self):
        assessment = clean_assessment(
            action_date=date(2026, 1, 1),
            measured_at=dt(2026, 6, 30),
            projected_horizon_days=30,
        )
        assert REASON_HORIZON_NOT_ELAPSED not in assessment.reasons

    def test_an_unknown_horizon_is_flagged_rather_than_assumed_met(self):
        assessment = clean_assessment(projected_horizon_days=None)
        assert assessment.verdict == VERDICT_WEAK
        assert REASON_HORIZON_UNKNOWN in assessment.reasons

    def test_elapsed_days_is_measured_from_the_action_date(self):
        assessment = clean_assessment(
            action_date=date(2026, 6, 1), measured_at=dt(2026, 6, 30)
        )
        assert assessment.elapsed_days_since_action == 29


# --------------------------------------------------------------------------
# 3. Sparse sampling and cadence gaps
# --------------------------------------------------------------------------


class TestSamplingAndCadence:
    def test_a_single_post_action_observation_is_weakly_comparable(self):
        assessment = clean_assessment(
            post_action_observation_count=1, post_action_run_dates=[dt(2026, 5, 10)]
        )
        assert assessment.verdict == VERDICT_WEAK
        assert REASON_SPARSE_SAMPLING in assessment.reasons

    def test_enough_observations_clears_the_sparse_flag(self):
        assessment = clean_assessment(post_action_observation_count=MIN_POST_ACTION_OBSERVATIONS)
        assert REASON_SPARSE_SAMPLING not in assessment.reasons

    def test_a_large_gap_between_runs_is_flagged(self):
        assessment = clean_assessment(
            post_action_run_dates=[dt(2026, 4, 1), dt(2026, 6, 30)]
        )
        assert assessment.verdict == VERDICT_WEAK
        assert REASON_CADENCE_GAP in assessment.reasons
        assert assessment.max_run_gap_days == 90

    def test_a_normal_monthly_cadence_is_not_flagged(self):
        """Calibration check: a routine cadence must not carry a permanent caveat.

        A threshold that fires on every monthly-cadence deployment produces a
        caveat people learn to ignore.
        """
        assessment = clean_assessment(
            post_action_run_dates=[dt(2026, 4, 1), dt(2026, 5, 1), dt(2026, 6, 1)]
        )
        assert REASON_CADENCE_GAP not in assessment.reasons
        assert assessment.max_run_gap_days <= MAX_RUN_GAP_DAYS


# --------------------------------------------------------------------------
# 4. Seasonality
# --------------------------------------------------------------------------


class TestSeasonality:
    def test_windows_in_different_seasons_are_flagged(self):
        assessment = clean_assessment(
            baseline_window_start=dt(2026, 1, 1),
            baseline_window_end=dt(2026, 3, 31),
            current_window_start=dt(2026, 7, 1),
            current_window_end=dt(2026, 9, 30),
        )
        assert assessment.verdict == VERDICT_WEAK
        assert REASON_SEASONALITY_MISMATCH in assessment.reasons
        assert assessment.seasonal_month_overlap == 0.0

    def test_the_same_months_a_year_apart_are_not_flagged(self):
        assessment = clean_assessment(
            baseline_window_start=dt(2025, 4, 1),
            baseline_window_end=dt(2025, 6, 30),
            current_window_start=dt(2026, 4, 1),
            current_window_end=dt(2026, 6, 30),
        )
        assert REASON_SEASONALITY_MISMATCH not in assessment.reasons
        assert assessment.seasonal_month_overlap == 1.0

    def test_overlap_is_reported_even_when_it_passes(self):
        assert clean_assessment().seasonal_month_overlap is not None


# --------------------------------------------------------------------------
# Boundary handling
# --------------------------------------------------------------------------


class TestBoundaryHandling:
    def test_the_boundary_treatment_is_always_stated(self):
        assert clean_assessment().boundary_handling == BOUNDARY_STRICTLY_AFTER

    def test_no_observations_is_stated_distinctly(self):
        assessment = clean_assessment(
            post_action_observation_count=0, post_action_run_dates=[]
        )
        assert assessment.boundary_handling == BOUNDARY_NO_OBSERVATIONS

    def test_a_baseline_window_extending_past_the_action_date_is_flagged(self):
        """Pre- and post-action observations are not cleanly separated."""
        assessment = clean_assessment(
            action_date=date(2026, 5, 1),
            baseline_window_start=dt(2026, 4, 1),
            baseline_window_end=dt(2026, 6, 30),
            projected_horizon_days=30,
            measured_at=dt(2026, 8, 1),
        )
        assert REASON_BOUNDARY_STRADDLE in assessment.reasons
        assert any("not cleanly separated" in n for n in assessment.notes)


# --------------------------------------------------------------------------
# Verdict escalation
# --------------------------------------------------------------------------


class TestVerdictEscalation:
    def test_a_verdict_only_ever_gets_worse(self):
        """A later check must not soften an earlier one."""
        assessment = clean_assessment(
            baseline_window_days=30,   # hard: not comparable
            current_window_days=9,
            post_action_observation_count=1,  # soft: weak
            post_action_run_dates=[dt(2026, 5, 10)],
        )
        assert assessment.verdict == VERDICT_NOT_COMPARABLE
        assert REASON_WINDOW_LENGTH_MISMATCH in assessment.reasons
        assert REASON_SPARSE_SAMPLING in assessment.reasons

    def test_multiple_soft_problems_stay_weak(self):
        assessment = clean_assessment(
            projected_horizon_days=None,
            post_action_observation_count=1,
            post_action_run_dates=[dt(2026, 5, 10)],
        )
        assert assessment.verdict == VERDICT_WEAK
        assert len(assessment.reasons) >= 2

    def test_every_reason_is_accompanied_by_a_readable_note_or_is_self_evident(self):
        assessment = clean_assessment(baseline_window_days=30, current_window_days=9)
        assert assessment.notes


# --------------------------------------------------------------------------
# Movement arithmetic
# --------------------------------------------------------------------------


class TestSignalMovements:
    def test_a_falling_signal_where_lower_is_better_improved(self):
        movements = build_signal_movements(
            [{"signalName": "owner_changes", "role": "movement", "value": 240}],
            {"owner_changes": 150},
        )
        m = movements[0]
        assert m.baseline_value == 240 and m.current_value == 150
        assert m.delta == -90
        assert m.delta_pct == pytest.approx(-37.5)
        assert m.direction == DIRECTION_IMPROVED

    def test_a_rising_signal_where_lower_is_better_worsened(self):
        movements = build_signal_movements(
            [{"signalName": "s", "value": 100}], {"s": 130}
        )
        assert movements[0].direction == DIRECTION_WORSENED

    def test_direction_respects_lower_is_better_false(self):
        """A coverage-style signal improves by going UP."""
        movements = build_signal_movements(
            [{"signalName": "coverage", "value": 40}],
            {"coverage": 70},
            lower_is_better_by_signal={"coverage": False},
        )
        assert movements[0].direction == DIRECTION_IMPROVED

    def test_a_tiny_change_reads_as_unchanged_not_as_movement(self):
        movements = build_signal_movements(
            [{"signalName": "s", "value": 1000}], {"s": 1005}
        )
        assert movements[0].direction == DIRECTION_UNCHANGED

    def test_a_signal_with_no_current_value_is_still_emitted(self):
        """Dropping it would make the record look complete while measuring less."""
        movements = build_signal_movements([{"signalName": "s", "value": 10}], {})
        assert len(movements) == 1
        assert movements[0].current_value is None
        assert movements[0].delta is None
        assert movements[0].direction == DIRECTION_UNKNOWN

    def test_a_zero_baseline_yields_no_percentage_rather_than_dividing_by_zero(self):
        movements = build_signal_movements([{"signalName": "s", "value": 0}], {"s": 5})
        assert movements[0].delta == 5
        assert movements[0].delta_pct is None

    def test_non_numeric_values_are_not_treated_as_measurements(self):
        movements = build_signal_movements(
            [{"signalName": "s", "value": "abc"}], {"s": True}
        )
        assert movements[0].baseline_value is None
        assert movements[0].current_value is None
        assert movements[0].direction == DIRECTION_UNKNOWN


# --------------------------------------------------------------------------
# The assembled record
# --------------------------------------------------------------------------


def baseline_artifact(**overrides):
    artifact = {
        "runId": "run_1",
        "capturedAt": "2026-04-01T00:00:00+00:00",
        "packVersion": "1.2.0",
        "window": {
            "days": 90,
            "startedAt": "2026-01-01T00:00:00+00:00",
            "endedAt": "2026-04-01T00:00:00+00:00",
        },
        "signals": [
            {"signalName": "owner_changes_90d", "role": "movement", "value": 240},
            {"signalName": "total_cases_90d", "role": "population", "value": 800},
        ],
        "measuredValues": {"owner_changes_90d": 240, "total_cases_90d": 800},
    }
    artifact.update(overrides)
    return artifact


def record(**overrides):
    kwargs = dict(
        org_id="org_1",
        opportunity_identity="ident_a",
        detector_id="HANDOFF_FRICTION",
        action_date=date(2026, 4, 15),
        baseline_artifact=baseline_artifact(),
        current_run_id="run_5",
        current_values={"owner_changes_90d": 150, "total_cases_90d": 790},
        current_window_days=90,
        current_window_start=dt(2026, 4, 20),
        current_window_end=dt(2026, 7, 19),
        current_pack_version="1.2.0",
        post_action_run_ids=["run_3", "run_4", "run_5"],
        post_action_run_dates=[dt(2026, 5, 1), dt(2026, 6, 1), dt(2026, 7, 1)],
        projected_horizon_days=30,
        measured_at=dt(2026, 7, 19),
    )
    kwargs.update(overrides)
    return build_movement_record(**kwargs)


class TestMovementRecord:
    def test_the_record_has_every_required_field(self):
        assert missing_movement_fields(record()) == []

    def test_missing_fields_are_named_for_an_incomplete_record(self):
        assert set(missing_movement_fields({})) >= set(REQUIRED_MOVEMENT_FIELDS)

    def test_comparability_is_never_null(self):
        assert "comparability.verdict" not in missing_movement_fields(record())
        assert record()["comparability"]["verdict"]

    def test_both_run_ids_are_first_class_fields(self):
        """AC7: an outcome number resolves to the runs on BOTH sides."""
        r = record()
        assert r["baselineRunId"] == "run_1"
        assert r["currentRunId"] == "run_5"

    def test_the_record_carries_baseline_current_and_delta(self):
        movement = record()["movements"][0]
        assert movement["baselineValue"] == 240
        assert movement["currentValue"] == 150
        assert movement["delta"] == -90

    def test_the_record_is_trace_friendly_not_a_flattened_number(self):
        """2.0-B1 expands an outcome claim and expects to land on records."""
        r = record()
        assert r["baseline"]["window"]["days"] == 90
        assert r["current"]["window"]["days"] == 90
        assert r["baseline"]["values"]
        assert r["current"]["values"]
        assert r["postActionRunIds"] == ["run_3", "run_4", "run_5"]
        assert len(r["movements"]) == 2, "each signal keeps its own before/after"

    def test_the_record_carries_both_pack_versions(self):
        """T4 compares them to flag pack-logic drift."""
        r = record(current_pack_version="1.3.0")
        assert r["baseline"]["packVersion"] == "1.2.0"
        assert r["current"]["packVersion"] == "1.3.0"

    def test_the_action_date_is_on_the_record(self):
        assert record()["actionDate"] == "2026-04-15"

    def test_a_poorly_comparable_record_still_reports(self):
        """Never a blocked measurement — the verdict rides along instead."""
        r = record(current_window_days=9, projected_horizon_days=365)
        assert r["comparability"]["verdict"] == VERDICT_NOT_COMPARABLE
        assert r["movements"][0]["delta"] == -90, "the delta is still reported"

    def test_the_record_is_json_serialisable(self):
        import json

        assert json.loads(json.dumps(record())) == record()

    def test_the_record_is_reproducible(self):
        assert record() == record()
