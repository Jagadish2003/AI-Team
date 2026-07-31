"""2.0-A2 T5 - projection validation verdicts as pure calibration data."""

from __future__ import annotations

from datetime import date, datetime, timezone

from app.projection_validation import (
    REASON_HORIZON_NOT_ELAPSED,
    REASON_PROJECTION_BAND_MISSING,
    REASON_PROJECTION_MISSING,
    VERDICT_ABOVE_BAND,
    VERDICT_BELOW_BAND,
    VERDICT_NOT_PROJECTED,
    VERDICT_TOO_EARLY,
    VERDICT_WITHIN_BAND,
    build_projection_validation,
    select_projection_entry_for_baseline,
    validation_filter_values,
)


def dt(y: int, m: int, d: int) -> datetime:
    return datetime(y, m, d, tzinfo=timezone.utc)


def movement_record(**overrides):
    record = {
        "orgId": "org_1",
        "opportunityIdentity": "ident_a",
        "detectorId": "HANDOFF_FRICTION",
        "actionDate": date(2026, 1, 1).isoformat(),
        "baselineRunId": "run_baseline",
        "currentRunId": "run_current",
        "measuredAt": dt(2026, 4, 15).isoformat(),
        "movements": [
            {
                "signalName": "owner_changes_90d",
                "role": "movement",
                "baselineValue": 240,
                "currentValue": 150,
                "delta": -90,
                "deltaPct": -37.5,
                "direction": "improved",
                "lowerIsBetter": True,
            }
        ],
        "confounders": [
            {
                "type": "pack_version_change",
                "severity": "material",
                "label": "Pack version changed",
            }
        ],
        "confounderSummary": {
            "count": 1,
            "materialCount": 1,
            "advisoryCount": 0,
            "byType": {"pack_version_change": 1},
            "types": ["pack_version_change"],
        },
    }
    record.update(overrides)
    return record


def projection(**overrides):
    payload = {
        "schemaVersion": "1.1.0",
        "direction": "improves",
        "magnitudeBand": {
            "lowPct": 25,
            "highPct": 55,
            "basisUnit": "of the recurring instances",
            "label": "25-55% of the recurring instances",
        },
        "observationHorizonDays": 30,
        "movementSignal": {
            "signalName": "owner_changes_90d",
            "unit": "count",
            "directionOfImprovement": "decrease",
        },
        "basis": {
            "detectorId": "HANDOFF_FRICTION",
            "packId": "service_cloud",
            "packVersion": "1.2.0",
            "confidence": "HIGH",
            "bandWidthModelVersion": "1.0.0",
        },
        "provenance": {
            "runId": "run_baseline",
            "createdAt": "2026-01-01T00:00:00+00:00",
            "packId": "service_cloud",
            "packVersion": "1.2.0",
        },
    }
    payload.update(overrides)
    return payload


class TestBandVerdicts:
    def test_measured_movement_inside_the_band_is_within_band(self):
        verdict = build_projection_validation(movement_record(), projection())
        assert verdict["verdict"] == VERDICT_WITHIN_BAND
        assert verdict["movementPct"] == 37.5

    def test_more_improvement_than_the_band_is_above_band(self):
        record = movement_record(
            movements=[
                {
                    "signalName": "owner_changes_90d",
                    "deltaPct": -70,
                    "lowerIsBetter": True,
                }
            ]
        )
        assert build_projection_validation(record, projection())["verdict"] == VERDICT_ABOVE_BAND

    def test_less_improvement_than_the_band_is_below_band(self):
        record = movement_record(
            movements=[
                {
                    "signalName": "owner_changes_90d",
                    "deltaPct": -12,
                    "lowerIsBetter": True,
                }
            ]
        )
        assert build_projection_validation(record, projection())["verdict"] == VERDICT_BELOW_BAND

    def test_higher_is_better_signals_reverse_the_delta_sign(self):
        record = movement_record(
            movements=[
                {
                    "signalName": "coverage_pct",
                    "deltaPct": 40,
                    "lowerIsBetter": False,
                }
            ]
        )
        proj = projection(
            movementSignal={"signalName": "coverage_pct", "unit": "pct"},
            magnitudeBand={"lowPct": 25, "highPct": 55},
        )
        assert build_projection_validation(record, proj)["verdict"] == VERDICT_WITHIN_BAND


class TestDistinctNonBandStates:
    def test_missing_projection_is_not_projected_not_below_band(self):
        verdict = build_projection_validation(movement_record(), None)
        assert verdict["verdict"] == VERDICT_NOT_PROJECTED
        assert verdict["reason"] == REASON_PROJECTION_MISSING

    def test_missing_band_is_not_projected_not_a_failure(self):
        verdict = build_projection_validation(
            movement_record(),
            projection(magnitudeBand=None),
        )
        assert verdict["verdict"] == VERDICT_NOT_PROJECTED
        assert verdict["reason"] == REASON_PROJECTION_BAND_MISSING

    def test_horizon_is_evaluated_before_the_band(self):
        verdict = build_projection_validation(
            movement_record(measuredAt=dt(2026, 1, 10).isoformat()),
            projection(observationHorizonDays=30),
        )
        assert verdict["verdict"] == VERDICT_TOO_EARLY
        assert verdict["reason"] == REASON_HORIZON_NOT_ELAPSED


class TestCalibrationPayload:
    def test_confounders_travel_with_the_verdict(self):
        verdict = build_projection_validation(movement_record(), projection())
        assert verdict["confounderSummary"]["count"] == 1
        assert verdict["confounders"][0]["type"] == "pack_version_change"

    def test_filter_values_promote_pack_detector_confidence_inputs(self):
        verdict = build_projection_validation(movement_record(), projection())
        values = validation_filter_values(verdict)
        assert values == {
            "verdict": VERDICT_WITHIN_BAND,
            "packId": "service_cloud",
            "packVersion": "1.2.0",
            "confidence": "HIGH",
        }

    def test_baseline_projection_selection_never_uses_a_later_projection(self):
        history = [
            {"runId": "run_later", "projection": projection()},
            {"runId": "run_baseline", "projection": projection(direction="improves")},
        ]
        selected = select_projection_entry_for_baseline("run_baseline", history)
        assert selected["runId"] == "run_baseline"
        assert select_projection_entry_for_baseline("missing", history) is None
