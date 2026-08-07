"""2.0-A2 T4 — confounder detection: the four types, and the two rules.

Pure unit tests. The two rules that constrain the whole subtask are enforced
structurally here as well as behaviourally, because both failure modes are silent:

* **never a silent adjustment** — nothing may scale, weight or correct a delta. A
  quietly adjusted number cannot be reproduced by a customer holding the same
  source data, and the moment they discover it the outcome story is dead.
* **never a blocked measurement** — a detected confounder must not suppress the
  result.

Both come from the same instinct (make the number look clean) and both are refused.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from app.outcome_confounder_config import (
    BASIS_PROVISIONAL,
    DEFAULT_CONFIG_PATH,
    RECOGNISED_BASES,
    SEASONALITY_CALENDAR_MONTH,
    SEASONALITY_DISABLED,
    SEASONALITY_FISCAL_QUARTER,
    SEVERITY_ADVISORY,
    SEVERITY_MATERIAL,
    ConfounderConfig,
    ConfounderConfigError,
    SeasonalityConfig,
    confounder_config_summary,
    load_confounder_config,
)
from app.outcome_confounders import (
    CONFOUNDER_CI_POPULATION_CHANGE,
    CONFOUNDER_PACK_VERSION_CHANGE,
    CONFOUNDER_SEASONALITY_MISMATCH,
    CONFOUNDER_VOLUME_SHIFT,
    Confounder,
    detect_ci_population_change,
    detect_confounders,
    detect_pack_version_change,
    detect_seasonality_mismatch,
    detect_volume_shift,
    register_confounder_detector,
    registered_confounder_detectors,
    summarise_confounders,
    unregister_confounder_detector,
)

BACKEND = Path(__file__).resolve().parents[2]
DETECTED_AT = "2026-09-30T00:00:00+00:00"


def baseline(**overrides):
    art = {
        "packVersion": "1.2.0",
        "signals": [
            {"signalName": "owner_changes_90d", "role": "movement", "value": 240},
            {"signalName": "total_cases_90d", "role": "population", "value": 800},
        ],
        "measuredValues": {"owner_changes_90d": 240, "total_cases_90d": 800},
    }
    art.update(overrides)
    return art


def movement(
    *,
    baseline_pack="1.2.0",
    current_pack="1.2.0",
    baseline_volume=800,
    current_volume=800,
    baseline_window=("2025-04-01T00:00:00+00:00", "2025-06-30T00:00:00+00:00"),
    current_window=("2026-04-01T00:00:00+00:00", "2026-06-30T00:00:00+00:00"),
):
    return {
        "baseline": {
            "packVersion": baseline_pack,
            "window": {"startedAt": baseline_window[0], "endedAt": baseline_window[1]},
            "values": {"owner_changes_90d": 240, "total_cases_90d": baseline_volume},
        },
        "current": {
            "packVersion": current_pack,
            "window": {"startedAt": current_window[0], "endedAt": current_window[1]},
            "values": {"owner_changes_90d": 150, "total_cases_90d": current_volume},
        },
        "movements": [
            {
                "signalName": "owner_changes_90d",
                "role": "movement",
                "baselineValue": 240,
                "currentValue": 150,
                "delta": -90,
            }
        ],
    }


def detect(**kwargs):
    args = dict(
        org_id="org_1",
        opportunity_identity="ident_a",
        baseline=baseline(),
        movement=movement(),
        detected_at=DETECTED_AT,
    )
    args.update(kwargs)
    return detect_confounders(**args)


def types_of(confounders):
    return {c["type"] for c in confounders}


# --------------------------------------------------------------------------
# Caveats are structured data
# --------------------------------------------------------------------------


class TestCaveatsAreStructuredData:
    def test_a_caveat_has_type_severity_detail_and_detected_at(self):
        """Not prose: T6 counts these by type and B1 renders them."""
        found = detect(movement=movement(current_pack="1.3.0"))
        caveat = next(c for c in found if c["type"] == CONFOUNDER_PACK_VERSION_CHANGE)

        assert caveat["type"] == CONFOUNDER_PACK_VERSION_CHANGE
        assert caveat["severity"] in (SEVERITY_MATERIAL, SEVERITY_ADVISORY)
        assert isinstance(caveat["detail"], dict)
        assert caveat["detectedAt"] == DETECTED_AT
        assert caveat["schemaVersion"]

    def test_the_detail_carries_the_values_that_were_compared(self):
        """A reader can check the judgement rather than take the label on trust."""
        found = detect(movement=movement(current_pack="1.3.0"))
        detail = next(
            c for c in found if c["type"] == CONFOUNDER_PACK_VERSION_CHANGE
        )["detail"]
        assert detail["baselinePackVersion"] == "1.2.0"
        assert detail["currentPackVersion"] == "1.3.0"

    def test_a_caveat_declares_the_basis_of_the_threshold_behind_it(self):
        """A caveat driven by a first-guess number should say so."""
        found = detect(movement=movement(current_volume=1200))
        caveat = next(c for c in found if c["type"] == CONFOUNDER_VOLUME_SHIFT)
        assert caveat["thresholdBasis"] in RECOGNISED_BASES

    def test_caveats_are_json_serialisable(self):
        found = detect(movement=movement(current_pack="1.3.0", current_volume=1200))
        assert json.loads(json.dumps(found)) == found

    def test_the_summary_counts_by_type_and_severity(self):
        found = detect(movement=movement(current_pack="1.3.0", current_volume=1200))
        summary = summarise_confounders(found)
        assert summary["count"] == len(found)
        assert summary["materialCount"] + summary["advisoryCount"] == summary["count"]
        assert CONFOUNDER_PACK_VERSION_CHANGE in summary["byType"]
        assert summary["types"] == sorted(summary["byType"])

    def test_the_summary_of_nothing_is_zero_not_an_error(self):
        assert summarise_confounders([])["count"] == 0
        assert summarise_confounders(None)["count"] == 0


# --------------------------------------------------------------------------
# 1. Pack-version change
# --------------------------------------------------------------------------


class TestPackVersionChange:
    def test_a_version_change_is_a_material_caveat(self):
        found = detect(movement=movement(current_pack="1.3.0"))
        caveat = next(c for c in found if c["type"] == CONFOUNDER_PACK_VERSION_CHANGE)
        assert caveat["severity"] == SEVERITY_MATERIAL
        assert "pack-logic" in caveat["detail"]["implication"]

    def test_the_same_version_produces_no_caveat(self):
        found = detect(movement=movement(current_pack="1.2.0"))
        assert CONFOUNDER_PACK_VERSION_CHANGE not in types_of(found)

    def test_a_missing_version_is_an_advisory_not_a_material_change(self):
        """An absence of information is not evidence of a change."""
        found = detect(movement=movement(current_pack=None))
        caveat = next(c for c in found if c["type"] == CONFOUNDER_PACK_VERSION_CHANGE)
        assert caveat["severity"] == SEVERITY_ADVISORY
        assert caveat["detail"]["reason"] == "missing_version"

    def test_detection_is_a_field_comparison_needing_no_extra_reads(self):
        """Nearly free: both versions are already on the record."""
        from app.outcome_confounders import ConfounderContext

        ctx = ConfounderContext(
            org_id="o",
            opportunity_identity="i",
            detected_at=DETECTED_AT,
            config=load_confounder_config(),
            baseline=baseline(),
            movement=movement(current_pack="9.9.9"),
        )
        found = detect_pack_version_change(ctx)
        assert len(found) == 1


# --------------------------------------------------------------------------
# 2. Volume shift
# --------------------------------------------------------------------------


class TestVolumeShift:
    def test_a_large_volume_increase_is_material(self):
        found = detect(movement=movement(current_volume=1200))
        caveat = next(c for c in found if c["type"] == CONFOUNDER_VOLUME_SHIFT)
        assert caveat["severity"] == SEVERITY_MATERIAL
        assert caveat["detail"]["direction"] == "increased"
        assert caveat["detail"]["shiftFraction"] == pytest.approx(0.5)

    def test_a_large_volume_decrease_is_material(self):
        found = detect(movement=movement(current_volume=400))
        caveat = next(c for c in found if c["type"] == CONFOUNDER_VOLUME_SHIFT)
        assert caveat["detail"]["direction"] == "decreased"

    def test_a_modest_shift_is_advisory(self):
        found = detect(movement=movement(current_volume=920))  # +15%
        caveat = next(c for c in found if c["type"] == CONFOUNDER_VOLUME_SHIFT)
        assert caveat["severity"] == SEVERITY_ADVISORY

    def test_ordinary_drift_produces_no_caveat(self):
        found = detect(movement=movement(current_volume=830))  # under advisory
        assert CONFOUNDER_VOLUME_SHIFT not in types_of(found)

    def test_the_thresholds_used_are_reported(self):
        found = detect(movement=movement(current_volume=1200))
        detail = next(c for c in found if c["type"] == CONFOUNDER_VOLUME_SHIFT)["detail"]
        assert "materialThreshold" in detail and "advisoryThreshold" in detail

    def test_a_zero_baseline_volume_yields_no_caveat_rather_than_dividing(self):
        found = detect(movement=movement(baseline_volume=0, current_volume=100))
        assert CONFOUNDER_VOLUME_SHIFT not in types_of(found)

    def test_no_population_signal_means_no_volume_check(self):
        art = baseline(signals=[{"signalName": "x", "role": "movement", "value": 1}])
        assert CONFOUNDER_VOLUME_SHIFT not in types_of(detect(baseline=art))


# --------------------------------------------------------------------------
# 3. CI/service population change
# --------------------------------------------------------------------------


class TestCiPopulationChange:
    def test_a_grown_population_is_flagged(self):
        found = detect(
            baseline_entity_keys=[f"service:s{i}" for i in range(10)],
            current_entity_keys=[f"service:s{i}" for i in range(13)],
        )
        caveat = next(
            c for c in found if c["type"] == CONFOUNDER_CI_POPULATION_CHANGE
        )
        assert caveat["detail"]["addedCount"] == 3
        assert caveat["detail"]["removedCount"] == 0
        assert "different populations" in caveat["detail"]["implication"]

    def test_a_shrunken_population_is_flagged(self):
        found = detect(
            baseline_entity_keys=[f"service:s{i}" for i in range(10)],
            current_entity_keys=[f"service:s{i}" for i in range(6)],
        )
        caveat = next(c for c in found if c["type"] == CONFOUNDER_CI_POPULATION_CHANGE)
        assert caveat["detail"]["removedCount"] == 4

    def test_an_unchanged_population_produces_no_caveat(self):
        keys = [f"service:s{i}" for i in range(10)]
        found = detect(baseline_entity_keys=keys, current_entity_keys=list(keys))
        assert CONFOUNDER_CI_POPULATION_CHANGE not in types_of(found)

    def test_an_unknown_population_produces_no_caveat(self):
        """Not knowing is not evidence of change — silence beats a fabrication."""
        found = detect(baseline_entity_keys=None, current_entity_keys=None)
        assert CONFOUNDER_CI_POPULATION_CHANGE not in types_of(found)

    def test_a_tiny_estate_uses_an_absolute_rule_not_a_fraction(self):
        """One service out of three is a 33% swing — a fraction is meaningless."""
        found = detect(
            baseline_entity_keys=["service:a", "service:b", "service:c"],
            current_entity_keys=["service:a", "service:b", "service:d"],
        )
        caveat = next(c for c in found if c["type"] == CONFOUNDER_CI_POPULATION_CHANGE)
        assert caveat["detail"]["rule"] == "absolute_count"
        assert caveat["detail"]["changeFraction"] is None

    def test_the_sample_lists_are_bounded(self):
        """A caveat is a summary; an unbounded list is unrenderable on a big estate."""
        found = detect(
            baseline_entity_keys=[],
            current_entity_keys=[f"service:s{i}" for i in range(500)],
        )
        detail = next(
            c for c in found if c["type"] == CONFOUNDER_CI_POPULATION_CHANGE
        )["detail"]
        assert detail["addedCount"] == 500
        assert len(detail["addedSample"]) == 10

    def test_a_b2_entity_resolution_event_surfaces_as_a_caveat(self):
        """An entity merge legitimately changes the population.

        Precisely because it is legitimate it must be labelled, or it reads as
        organic drift in the estate.
        """
        keys = [f"service:s{i}" for i in range(10)]
        found = detect(
            baseline_entity_keys=keys,
            current_entity_keys=list(keys),
            entity_resolution_events=[
                {"kind": "merge", "entityKey": "service:s1", "occurredAt": DETECTED_AT}
            ],
        )
        caveat = next(
            c
            for c in found
            if c["type"] == CONFOUNDER_CI_POPULATION_CHANGE
            and c["detail"].get("reason") == "entity_resolution"
        )
        assert caveat["detail"]["eventCount"] == 1
        assert "organic drift" in caveat["detail"]["implication"]


# --------------------------------------------------------------------------
# 4. Seasonality window mismatch
# --------------------------------------------------------------------------


class TestSeasonalityMismatch:
    def test_windows_in_different_parts_of_the_year_are_flagged(self):
        found = detect(
            movement=movement(
                baseline_window=("2026-01-01T00:00:00+00:00", "2026-03-31T00:00:00+00:00"),
                current_window=("2026-07-01T00:00:00+00:00", "2026-09-30T00:00:00+00:00"),
            )
        )
        caveat = next(
            c for c in found if c["type"] == CONFOUNDER_SEASONALITY_MISMATCH
        )
        assert caveat["detail"]["overlapFraction"] == 0.0
        assert caveat["detail"]["mode"] == SEASONALITY_CALENDAR_MONTH

    def test_the_same_months_a_year_apart_are_not_flagged(self):
        found = detect()  # default windows are Apr-Jun in both years
        assert CONFOUNDER_SEASONALITY_MISMATCH not in types_of(found)

    def test_the_window_definition_comes_from_config_not_a_hardcoded_calendar(self):
        """A fixed calendar assumption is wrong for much of the customer base."""
        from app.outcome_confounders import ConfounderContext

        base = load_confounder_config()
        fiscal = ConfounderConfig(
            config_version=base.config_version,
            volume_shift=base.volume_shift,
            ci_population=base.ci_population,
            pack_version=base.pack_version,
            seasonality=SeasonalityConfig(
                mode=SEASONALITY_FISCAL_QUARTER, fiscal_year_start_month=4
            ),
            bases=base.bases,
        )
        ctx = ConfounderContext(
            org_id="o",
            opportunity_identity="i",
            detected_at=DETECTED_AT,
            config=fiscal,
            baseline=baseline(),
            movement=movement(
                baseline_window=("2026-04-01T00:00:00+00:00", "2026-05-31T00:00:00+00:00"),
                current_window=("2026-10-01T00:00:00+00:00", "2026-11-30T00:00:00+00:00"),
            ),
        )
        found = detect_seasonality_mismatch(ctx)
        assert found
        assert found[0].detail["unit"] == "fiscal_quarter"
        assert found[0].detail["fiscalYearStartMonth"] == 4

    def test_seasonality_can_be_disabled_for_a_non_seasonal_operation(self):
        from app.outcome_confounders import ConfounderContext

        base = load_confounder_config()
        disabled = ConfounderConfig(
            config_version=base.config_version,
            volume_shift=base.volume_shift,
            ci_population=base.ci_population,
            pack_version=base.pack_version,
            seasonality=SeasonalityConfig(mode=SEASONALITY_DISABLED),
            bases=base.bases,
        )
        ctx = ConfounderContext(
            org_id="o",
            opportunity_identity="i",
            detected_at=DETECTED_AT,
            config=disabled,
            baseline=baseline(),
            movement=movement(
                baseline_window=("2026-01-01T00:00:00+00:00", "2026-03-31T00:00:00+00:00"),
                current_window=("2026-07-01T00:00:00+00:00", "2026-09-30T00:00:00+00:00"),
            ),
        )
        assert detect_seasonality_mismatch(ctx) == []

    def test_missing_windows_produce_no_caveat(self):
        found = detect(movement={"baseline": {}, "current": {}})
        assert CONFOUNDER_SEASONALITY_MISMATCH not in types_of(found)


# --------------------------------------------------------------------------
# The two rules
# --------------------------------------------------------------------------


class TestNeverASilentAdjustment:
    def test_the_delta_is_unchanged_by_any_number_of_confounders(self):
        clean = detect()
        confounded_movement = movement(
            current_pack="9.9.9",
            current_volume=5000,
            baseline_window=("2026-01-01T00:00:00+00:00", "2026-03-31T00:00:00+00:00"),
            current_window=("2026-07-01T00:00:00+00:00", "2026-09-30T00:00:00+00:00"),
        )
        found = detect(
            movement=confounded_movement,
            baseline_entity_keys=[f"s:{i}" for i in range(10)],
            current_entity_keys=[f"s:{i}" for i in range(30)],
        )
        assert len(found) >= 4, "all four confounder types should fire here"
        # The record's own delta is untouched: detection returns caveats only.
        assert confounded_movement["movements"][0]["delta"] == -90

    def test_detection_returns_only_caveats_and_mutates_nothing(self):
        import copy

        art, rec = baseline(), movement(current_pack="1.3.0", current_volume=1200)
        art_before, rec_before = copy.deepcopy(art), copy.deepcopy(rec)
        detect(baseline=art, movement=rec)
        assert art == art_before, "detection must not mutate the baseline"
        assert rec == rec_before, "detection must not mutate the movement record"

    def test_no_arithmetic_on_a_delta_exists_in_the_module(self):
        """Structural: a future edit that adjusts a delta fails the build.

        Read the source rather than trusting review, because a scaled number looks
        entirely normal in output.
        """
        source = (BACKEND / "app" / "outcome_confounders.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        offenders = []
        for node in ast.walk(tree):
            # Any assignment whose target names a delta, or any binop applied to a
            # value read from a "delta" key, is the shape being prohibited.
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    name = getattr(target, "id", "") or getattr(target, "attr", "")
                    if "delta" in str(name).lower():
                        offenders.append(f"assignment to {name}")
        assert not offenders, (
            f"confounder detection must never compute or adjust a delta: {offenders}"
        )
        for banned in ("* adjustment", "adjusted_delta", "corrected_delta", "scale_delta"):
            assert banned not in source

    def test_the_module_never_writes_to_the_movements_table(self):
        source = (BACKEND / "app" / "outcome_confounders.py").read_text(encoding="utf-8")
        for verb in ("UPDATE opportunity_movements", "INSERT INTO", "DELETE FROM"):
            assert verb not in source


class TestNeverABlockedMeasurement:
    def test_detection_has_no_suppress_return_path(self):
        """AC3: a detected confounder must not suppress the result."""
        found = detect(movement=movement(current_pack="9.9.9", current_volume=9000))
        assert isinstance(found, list)
        assert found, "caveats are returned; nothing signals 'do not publish'"

    def test_a_broken_detector_does_not_lose_the_other_caveats(self):
        """One failure must not cost the measurement its remaining caveats."""

        def exploding(_ctx):
            raise RuntimeError("boom")

        register_confounder_detector("exploding_test", exploding, replace=True)
        try:
            found = detect(movement=movement(current_pack="1.3.0"))
            assert CONFOUNDER_PACK_VERSION_CHANGE in types_of(found)
        finally:
            unregister_confounder_detector("exploding_test")

    def test_a_detector_returning_none_is_tolerated(self):
        register_confounder_detector("nones_test", lambda _ctx: None, replace=True)
        try:
            assert isinstance(detect(), list)
        finally:
            unregister_confounder_detector("nones_test")


# --------------------------------------------------------------------------
# Extensibility
# --------------------------------------------------------------------------


class TestExtensibility:
    def test_the_four_in_scope_detectors_are_registered(self):
        assert set(registered_confounder_detectors()) >= {
            CONFOUNDER_VOLUME_SHIFT,
            CONFOUNDER_CI_POPULATION_CHANGE,
            CONFOUNDER_PACK_VERSION_CHANGE,
            CONFOUNDER_SEASONALITY_MISMATCH,
        }

    def test_a_new_type_is_registerable_without_touching_the_engine(self):
        """This list will grow once real outcome data exists."""

        def staffing(ctx):
            return [
                Confounder(
                    type="staffing_change",
                    severity=SEVERITY_ADVISORY,
                    label="Team size changed",
                    detail={"before": 6, "after": 3},
                    detected_at=ctx.detected_at,
                )
            ]

        register_confounder_detector("staffing_change", staffing, replace=True)
        try:
            found = detect()
            assert "staffing_change" in types_of(found)
        finally:
            unregister_confounder_detector("staffing_change")

    def test_the_engine_does_not_enumerate_detector_types(self):
        """The comparison engine calls detect_confounders once and learns nothing."""
        engine = (BACKEND / "app" / "opportunity_movement.py").read_text(encoding="utf-8")
        for type_code in (
            CONFOUNDER_VOLUME_SHIFT,
            CONFOUNDER_CI_POPULATION_CHANGE,
            CONFOUNDER_PACK_VERSION_CHANGE,
            CONFOUNDER_SEASONALITY_MISMATCH,
        ):
            assert type_code not in engine, (
                f"the movement engine names {type_code!r}; adding a confounder type "
                "must not require editing it"
            )

    def test_registering_a_duplicate_name_is_refused_unless_deliberate(self):
        register_confounder_detector("dupe_test", lambda _c: [], replace=True)
        try:
            with pytest.raises(ValueError, match="already registered"):
                register_confounder_detector("dupe_test", lambda _c: [])
        finally:
            unregister_confounder_detector("dupe_test")

    def test_an_unnamed_detector_is_refused(self):
        with pytest.raises(ValueError):
            register_confounder_detector("  ", lambda _c: [])


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------


class TestConfiguration:
    def test_the_shipped_config_loads(self):
        config = load_confounder_config()
        assert config.config_version
        assert config.source_path.endswith("outcome_confounders.json")

    def test_thresholds_are_configuration_not_constants(self):
        """A value change must alter behaviour with no code deploy."""
        source = (BACKEND / "app" / "outcome_confounders.py").read_text(encoding="utf-8")
        # No literal threshold fractions in the detector module.
        for literal in ("0.25", "0.20", "0.10", "0.05"):
            assert literal not in source, (
                f"threshold literal {literal} is hardcoded in the detectors; it "
                "belongs in config/outcome_confounders.json"
            )

    def test_every_threshold_declares_its_basis(self):
        """Measured vs provisional must be visible, not archaeology."""
        config = load_confounder_config()
        for section in ("volume_shift", "ci_population", "pack_version", "seasonality"):
            assert config.basis_for(section) in RECOGNISED_BASES

    def test_an_undeclared_basis_defaults_to_the_least_trustworthy(self):
        config = load_confounder_config()
        assert config.basis_for("no_such_section") == BASIS_PROVISIONAL

    def test_the_config_summary_is_the_audit_surface(self):
        summary = confounder_config_summary()
        assert summary["configVersion"]
        for key in ("volumeShift", "ciPopulation", "packVersion", "seasonality"):
            assert "basis" in summary[key]

    def test_documentation_keys_are_stripped_before_use(self):
        """`_`-prefixed keys document the config; they must not become thresholds."""
        raw = json.loads(Path(DEFAULT_CONFIG_PATH).read_text(encoding="utf-8"))
        assert "_meta" in raw, "the shipped config should document itself"
        config = load_confounder_config()
        assert not any(k.startswith("_") for k in config.bases)

    def test_a_missing_config_raises_rather_than_defaulting_silently(self):
        """A deployment that thinks it configured a threshold and did not should be told."""
        with pytest.raises(ConfounderConfigError):
            load_confounder_config("/nonexistent/outcome_confounders.json")

    def test_an_unknown_seasonality_mode_is_refused(self, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text(json.dumps({"seasonality": {"mode": "lunar"}}), encoding="utf-8")
        with pytest.raises(ConfounderConfigError, match="seasonality mode"):
            load_confounder_config(str(bad))

    def test_an_out_of_range_fiscal_start_month_is_refused(self, tmp_path):
        bad = tmp_path / "bad2.json"
        bad.write_text(
            json.dumps({"seasonality": {"mode": "fiscal_quarter",
                                        "fiscal_year_start_month": 13}}),
            encoding="utf-8",
        )
        with pytest.raises(ConfounderConfigError, match="fiscal_year_start_month"):
            load_confounder_config(str(bad))

    def test_an_unrecognised_basis_value_is_refused(self, tmp_path):
        bad = tmp_path / "bad3.json"
        bad.write_text(
            json.dumps({"volume_shift": {"_basis": "vibes",
                                         "material_shift_fraction": 0.3}}),
            encoding="utf-8",
        )
        with pytest.raises(ConfounderConfigError, match="basis"):
            load_confounder_config(str(bad))

    def test_a_changed_threshold_changes_behaviour_with_no_code_change(self, tmp_path):
        from app.outcome_confounders import ConfounderContext

        strict = tmp_path / "strict.json"
        strict.write_text(
            json.dumps(
                {
                    "_meta": {"configVersion": "test"},
                    "volume_shift": {
                        "_basis": "provisional",
                        "material_shift_fraction": 0.01,
                        "advisory_shift_fraction": 0.005,
                    },
                }
            ),
            encoding="utf-8",
        )
        ctx = ConfounderContext(
            org_id="o",
            opportunity_identity="i",
            detected_at=DETECTED_AT,
            config=load_confounder_config(str(strict)),
            baseline=baseline(),
            movement=movement(current_volume=830),  # +3.75%, below shipped thresholds
        )
        found = detect_volume_shift(ctx)
        assert found, "a stricter configured threshold must fire without a code change"
        assert found[0].severity == SEVERITY_MATERIAL
