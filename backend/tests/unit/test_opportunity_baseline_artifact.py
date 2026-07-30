"""2.0-A2 T2 — the frozen baseline artifact's contents and reproducibility.

Pure unit tests: the builder takes ``captured_at`` as an argument precisely so the
artifact is reproducible and these can run without a DB or a frozen clock.

What is being protected: **every field T3's comparison and T4's confounder checks
need must be present at capture**. If any of them is missing, the failure surfaces
months later as an outcome claim that cannot be defended — so the required set is
asserted here rather than discovered then.
"""

from __future__ import annotations

import copy

import pytest

from app.opportunity_baseline_artifact import (
    BASELINE_SCHEMA_VERSION,
    DEFAULT_WINDOW_DAYS,
    REQUIRED_ARTIFACT_FIELDS,
    ROLE_INSTANCE,
    ROLE_MEASURED,
    ROLE_MOVEMENT,
    ROLE_POPULATION,
    WINDOW_FROM_DETECTOR_DEFAULT,
    WINDOW_FROM_TEMPORAL_BASELINE,
    BaselineCaptureError,
    build_baseline_artifact,
    missing_artifact_fields,
)

CAPTURED_AT = "2026-07-30T10:05:00+00:00"
RUN_COMPLETED = "2026-07-30T10:00:00+00:00"


def seeded_opp(**overrides):
    opp = {
        "id": "opp_001",
        "opportunity_identity": "opp_stable_abc123",
        "packId": "service_cloud",
        "packVersion": "1.2.0",
        "confidence": "HIGH",
        "baseline_mean": 201.6,
        "baseline_stddev": 2.7,
        "baseline_window_days": 90,
        "run_count": 5,
        "current_value": 2.4,
        "recent_values": [200.0, 205.0, 198.0, 202.0, 203.0],
        "signal_key": "service_cloud::HANDOFF_FRICTION::metric_value",
        "_debug": {
            "detector_id": "HANDOFF_FRICTION",
            "metric_value": 2.4,
            "threshold": 1.5,
            "run_completed_at": RUN_COMPLETED,
            "raw_evidence": {
                "owner_changes_90d": 240.0,
                "total_cases_90d": 800.0,
                "handoff_score": 2.4,
            },
        },
    }
    opp.update(overrides)
    return opp


def build(opp=None, **kw):
    kwargs = {"org_id": "org_1", "run_id": "run_1", "captured_at": CAPTURED_AT}
    kwargs.update(kw)
    return build_baseline_artifact(opp or seeded_opp(), **kwargs)


# --------------------------------------------------------------------------
# Completeness — what T3 and T4 depend on.
# --------------------------------------------------------------------------


class TestArtifactCompleteness:
    def test_every_required_field_is_present(self):
        assert missing_artifact_fields(build()) == []

    def test_missing_fields_are_named_for_an_incomplete_artifact(self):
        assert set(missing_artifact_fields({})) == set(REQUIRED_ARTIFACT_FIELDS)
        assert missing_artifact_fields(None)

    def test_the_artifact_records_which_signals_the_finding_rests_on(self):
        signals = {s["signalName"]: s for s in build()["signals"]}
        assert "owner_changes_90d" in signals
        assert signals["owner_changes_90d"]["role"] == ROLE_MOVEMENT
        assert signals["owner_changes_90d"]["value"] == 240
        assert signals["total_cases_90d"]["role"] == ROLE_POPULATION

    def test_signal_roles_come_from_the_a1_registry_not_a_guess(self):
        """T3 must re-measure the SAME fields the detector actually emits."""
        from discovery.projection.signal_registry import get_detector_profile

        profile = get_detector_profile("HANDOFF_FRICTION")
        roles = {s["signalName"]: s["role"] for s in build()["signals"]}
        assert roles[profile.movement_signal] == ROLE_MOVEMENT
        assert roles[profile.volume_signal] == ROLE_POPULATION

    def test_unprofiled_measured_fields_are_still_captured(self):
        """A field the profile does not name is better stored than lost."""
        roles = {s["signalName"]: s["role"] for s in build()["signals"]}
        assert roles["handoff_score"] == ROLE_MEASURED

    def test_the_artifact_records_the_measured_values(self):
        values = build()["measuredValues"]
        assert values == {
            "handoff_score": 2.4,
            "owner_changes_90d": 240,
            "total_cases_90d": 800,
        }

    def test_the_artifact_records_the_pack_version(self):
        """T4's confounder detection reads exactly this field.

        Capture it here or pack-logic drift between the two measurements is
        undetectable.
        """
        artifact = build()
        assert artifact["packVersion"] == "1.2.0"
        assert artifact["packId"] == "service_cloud"

    def test_the_artifact_records_the_run_and_detector_identity(self):
        artifact = build()
        assert artifact["runId"] == "run_1"
        assert artifact["detectorId"] == "HANDOFF_FRICTION"
        assert artifact["orgId"] == "org_1"
        assert artifact["opportunityIdentity"] == "opp_stable_abc123"

    def test_the_artifact_references_the_originating_instance(self):
        """A reference, not a replacement.

        The instance row is "how the finding scored on that run"; this artifact is
        "the measurement basis we will be judged against".
        """
        ref = build()["instanceRef"]
        assert ref == {"opportunityIdentity": "opp_stable_abc123", "runId": "run_1"}

    def test_the_artifact_freezes_the_rolling_temporal_statistic(self):
        """baseline_calculator keeps moving these; the frozen copy does not."""
        stats = build()["baselineStats"]
        assert stats["mean"] == 201.6
        assert stats["stddev"] == 2.7
        assert stats["windowDays"] == 90
        assert stats["runCount"] == 5
        assert stats["currentValue"] == 2.4
        assert stats["recentValues"] == [200, 205, 198, 202, 203]
        assert stats["metricValue"] == 2.4
        assert stats["threshold"] == 1.5
        assert stats["confidence"] == "HIGH"

    def test_the_schema_version_is_stamped(self):
        assert build()["schemaVersion"] == BASELINE_SCHEMA_VERSION


# --------------------------------------------------------------------------
# The observation window.
# --------------------------------------------------------------------------


class TestObservationWindow:
    def test_the_window_records_start_end_and_how_it_was_derived(self):
        window = build()["window"]
        assert window["days"] == 90
        assert window["endedAt"] == RUN_COMPLETED
        assert window["startedAt"] == "2026-05-01T10:00:00+00:00"
        assert window["derivation"] == WINDOW_FROM_TEMPORAL_BASELINE

    def test_a_fallback_window_is_labelled_as_such(self):
        """A fallback and a measured window must never look identical."""
        opp = seeded_opp()
        opp.pop("baseline_window_days")
        window = build(opp)["window"]
        assert window["days"] == DEFAULT_WINDOW_DAYS
        assert window["derivation"] == WINDOW_FROM_DETECTOR_DEFAULT

    def test_the_window_end_falls_back_to_the_capture_time(self):
        opp = seeded_opp()
        opp["_debug"].pop("run_completed_at")
        window = build(opp)["window"]
        assert window["endedAt"] == CAPTURED_AT

    def test_the_window_span_matches_its_declared_days(self):
        from datetime import datetime

        window = build()["window"]
        start = datetime.fromisoformat(window["startedAt"])
        end = datetime.fromisoformat(window["endedAt"])
        assert (end - start).days == window["days"]


# --------------------------------------------------------------------------
# Reproducibility.
# --------------------------------------------------------------------------


class TestReproducibility:
    def test_the_same_opportunity_produces_an_identical_artifact(self):
        opp = seeded_opp()
        first = build(copy.deepcopy(opp))
        for _ in range(4):
            assert build(copy.deepcopy(opp)) == first

    def test_the_artifact_does_not_depend_on_dict_ordering(self):
        base = seeded_opp()
        reordered = {k: base[k] for k in sorted(base)}
        assert build(reordered) == build(base)

    def test_the_builder_reads_no_clock(self):
        """captured_at is supplied, so two builds a second apart are identical."""
        opp = seeded_opp()
        assert build(opp, captured_at=CAPTURED_AT) == build(opp, captured_at=CAPTURED_AT)

    def test_a_different_capture_time_changes_only_the_timestamp(self):
        later = "2026-07-30T11:00:00+00:00"
        a, b = build(), build(captured_at=later)
        assert a["capturedAt"] != b["capturedAt"]
        for key in ("signals", "measuredValues", "baselineStats", "packVersion"):
            assert a[key] == b[key]


# --------------------------------------------------------------------------
# Refusals — an artifact that cannot be matched later is worse than none.
# --------------------------------------------------------------------------


class TestRefusals:
    def test_an_opportunity_without_a_stable_identity_is_refused(self):
        opp = seeded_opp()
        opp.pop("opportunity_identity")
        with pytest.raises(BaselineCaptureError) as excinfo:
            build(opp)
        assert "opportunity_identity" in str(excinfo.value)

    def test_a_blank_identity_is_refused(self):
        with pytest.raises(BaselineCaptureError):
            build(seeded_opp(opportunity_identity="   "))

    def test_an_opportunity_without_a_detector_is_refused(self):
        """T4 cannot compare a measurement whose detector is unknown."""
        opp = seeded_opp()
        opp["_debug"].pop("detector_id")
        with pytest.raises(BaselineCaptureError) as excinfo:
            build(opp)
        assert "detector_id" in str(excinfo.value)

    def test_a_detector_with_no_registry_profile_still_gets_a_baseline(self):
        """Silently skipping findings would make them quietly unmeasurable."""
        opp = seeded_opp()
        opp["_debug"]["detector_id"] = "SOME_UNPROFILED_DETECTOR"
        artifact = build(opp)
        assert artifact["detectorId"] == "SOME_UNPROFILED_DETECTOR"
        assert artifact["measuredValues"], "raw measured evidence is still frozen"
        assert all(s["role"] == ROLE_MEASURED for s in artifact["signals"])

    def test_an_opportunity_with_no_raw_evidence_still_captures_its_basis(self):
        """The temporal statistics are frozen even with no raw evidence...

        ...but the artifact reports itself INCOMPLETE, because T3 has no measured
        field to re-measure. Better a baseline that says what it lacks than one
        that looks whole.
        """
        opp = seeded_opp()
        opp["_debug"]["raw_evidence"] = {}
        artifact = build(opp)

        assert artifact["measuredValues"] == {}
        assert artifact["baselineStats"]["mean"] == 201.6
        assert missing_artifact_fields(artifact) == ["measuredValues"], (
            "an artifact with nothing to re-measure must declare that gap"
        )

    def test_non_numeric_and_boolean_evidence_is_not_treated_as_a_measurement(self):
        opp = seeded_opp()
        opp["_debug"]["raw_evidence"] = {
            "count": 5,
            "label": "not a number",
            "flag": True,
            "nothing": None,
        }
        values = build(opp)["measuredValues"]
        assert values == {"count": 5}, "only real numbers are measurements"


# --------------------------------------------------------------------------
# Shape stability — the contract T3/T4 are written against.
# --------------------------------------------------------------------------


class TestShapeStability:
    def test_the_required_field_set_is_what_t3_and_t4_need(self):
        """A guard on the guard: this tuple is the promise to later subtasks."""
        assert set(REQUIRED_ARTIFACT_FIELDS) >= {
            "opportunityIdentity",  # which problem
            "runId",                # which run produced the basis
            "detectorId",           # what measured it
            "packVersion",          # T4's pack-drift check
            "signals",              # which signals (T3 re-measures these)
            "window",               # over which window
            "baselineStats",        # at which values
            "capturedAt",           # when it was frozen
        }

    def test_pack_version_is_required_as_a_key_even_when_absent(self):
        """A pack that never stamped a version must still be visibly unstamped."""
        opp = seeded_opp()
        opp.pop("packVersion")
        artifact = build(opp)
        assert "packVersion" in artifact
        assert artifact["packVersion"] is None
        assert "packVersion" not in missing_artifact_fields(artifact)

    def test_the_artifact_is_json_serialisable(self):
        import json

        assert json.loads(json.dumps(build())) == build()
