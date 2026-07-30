"""2.0-A1 T4 — deterministic band-width calculation, on seeded findings.

The story's demand, restated: band width comes from EVIDENCE QUALITY, not from
manual configuration. These tests pin that as behaviour rather than as intent:

  * the same seeded finding always yields the same band (AC2, AC5);
  * thinner evidence yields a demonstrably WIDER band on every axis (AC2);
  * stronger corroboration yields a demonstrably NARROWER band (AC2);
  * a capped (single-source) finding is LABELLED and never out-ranks a
    corroborated equivalent on projection strength alone (AC4);
  * nothing the model emits is a point estimate or a savings claim (AC3).

Every finding here is seeded — literal, checked-in evidence values — so a
failure names a rule that broke, not an environment that drifted. Pure unit
tests: the band-width model touches no DB, no clock, and no network.
"""

from __future__ import annotations

import copy

import pytest

from discovery.projection import build_projection
from discovery.projection.band_width import (
    AXIS_CONFIDENCE_CAP,
    AXIS_CORROBORATION,
    AXIS_RECURRENCE_STABILITY,
    AXIS_SAMPLE_SIZE,
    AXIS_WEIGHTS,
    BAND_WIDTH_MODEL_VERSION,
    CAPPED_STRENGTH_CEILING,
    CAPPED_STRENGTH_LABEL,
    CORROBORATION_CORROBORATED,
    CORROBORATION_SINGLE_SOURCE,
    CORROBORATION_SUPPORTING_ONLY,
    CORROBORATION_TRIPLE,
    STABILITY_BURSTY,
    STABILITY_STEADY,
    STABILITY_UNKNOWN,
    STABILITY_VARIABLE,
    BandWidthInputs,
    compute_band_width,
    demote_capped_projections,
    order_by_projection_strength,
    projection_is_capped,
    projection_rank_key,
    projection_strength_of,
)


# --------------------------------------------------------------------------
# Seeded findings
# --------------------------------------------------------------------------

#: A steady 90-day series — coefficient of variation well under the steady
#: threshold, so recurrence stability is unambiguous.
STEADY_SERIES = [200.0, 205.0, 198.0, 202.0, 203.0]

#: A bursty series — the same mean order of magnitude, wildly variable.
BURSTY_SERIES = [10.0, 400.0, 25.0, 380.0, 15.0]

#: A middling series — variable but not bursty (CV ~0.33, between the two
#: documented thresholds).
VARIABLE_SERIES = [100.0, 200.0, 150.0, 250.0, 120.0]


def seeded_finding(
    *,
    total_cases=800.0,
    owner_changes=240.0,
    recent_values=None,
    corroboration_sources=("ServiceNow", "Jira"),
    corroboration_rule_ids=("COR-01", "COR-02"),
    triple=False,
    confidence="HIGH",
):
    """The reference seeded finding — strong evidence on all four axes.

    Shaped exactly as the Track A adapter stores an opportunity (numeric
    ``raw_evidence`` under ``_debug``), so these tests exercise the same read
    path production does.
    """
    return {
        "id": "opp_seed_001",
        "title": "Elevated case reassignment",
        "confidence": confidence,
        "impact": 8,
        "effort": 3,
        "tier": "Quick Win",
        "evidenceIds": ["ev_sf_seed001"],
        "corroboration_sources": list(corroboration_sources),
        "corroboration_rule_ids": list(corroboration_rule_ids),
        "triple_corroboration": triple,
        "packId": "service_cloud",
        "packVersion": "1.2.0",
        "recent_values": list(STEADY_SERIES if recent_values is None else recent_values),
        "baseline_mean": 202.0,
        "baseline_stddev": 4.0,
        "baseline_window_days": 90,
        "run_count": 5,
        "signal_key": "service_cloud::HANDOFF_FRICTION::metric_value",
        "_debug": {
            "detector_id": "HANDOFF_FRICTION",
            "signal_source": "salesforce",
            "metric_value": 2.4,
            "threshold": 1.5,
            "roadmap_stage": "NEXT_30",
            "raw_evidence": {
                "owner_changes_90d": owner_changes,
                "total_cases_90d": total_cases,
                "handoff_score": 2.4,
            },
        },
    }


def band_of(finding):
    projection = build_projection(finding)
    assert projection is not None, "seeded finding must be projectable"
    return projection


def width_of(projection):
    band = projection["magnitudeBand"]
    return band["highPct"] - band["lowPct"]


def inputs(
    sample_size=400.0,
    stability=STABILITY_STEADY,
    corroboration=CORROBORATION_CORROBORATED,
    capped=False,
):
    return BandWidthInputs(
        sample_size=sample_size,
        recurrence_stability=stability,
        corroboration_status=corroboration,
        confidence_capped=capped,
    )


# --------------------------------------------------------------------------
# AC2 — the same seeded finding always produces the same band.
# --------------------------------------------------------------------------


class TestDeterminism:
    def test_same_seeded_finding_yields_an_identical_band_every_time(self):
        finding = seeded_finding()
        first = build_projection(copy.deepcopy(finding))
        for _ in range(5):
            assert build_projection(copy.deepcopy(finding)) == first

    def test_band_does_not_depend_on_dict_ordering_or_extra_fields(self):
        """Reordering keys or carrying unrelated fields must not move a band."""
        base = seeded_finding()
        reordered = {k: base[k] for k in sorted(base)}
        reordered["some_unrelated_field"] = "ignored"
        assert build_projection(reordered)["bandWidth"] == build_projection(base)[
            "bandWidth"
        ]

    def test_band_width_is_a_pure_function_of_the_four_inputs(self):
        """Two findings differing ONLY in irrelevant fields band identically."""
        a = seeded_finding()
        b = seeded_finding()
        b["title"] = "A completely different title"
        b["impact"] = 2
        b["effort"] = 9
        b["tier"] = "Complex"
        b["packVersion"] = "9.9.9"
        assert build_projection(a)["bandWidth"] == build_projection(b)["bandWidth"]

    def test_computed_band_is_stamped_with_the_model_version(self):
        assert (
            band_of(seeded_finding())["bandWidth"]["modelVersion"]
            == BAND_WIDTH_MODEL_VERSION
        )

    def test_every_emitted_number_is_rounded_for_stable_storage(self):
        """AC5: a stored band must compare byte-for-byte with a recomputed one."""
        band_width = band_of(seeded_finding(total_cases=37.0))["bandWidth"]
        for key in ("halfWidth", "evidencePenalty", "evidenceQuality"):
            assert band_width[key] == round(band_width[key], 4), key
        for driver in band_width["drivers"]:
            assert driver["widensByPct"] == round(driver["widensByPct"], 2)


# --------------------------------------------------------------------------
# AC2 — thinner evidence widens; stronger evidence narrows. Per axis.
# --------------------------------------------------------------------------


class TestSampleSizeAxis:
    @pytest.mark.parametrize(
        "population, expected_tier",
        [
            (800.0, "strong"),
            (40.0, "moderate"),
            (12.0, "thin"),
            (5.0, "minimal"),
        ],
    )
    def test_sample_size_buckets_as_documented(self, population, expected_tier):
        projection = band_of(
            seeded_finding(total_cases=population, owner_changes=population / 2)
        )
        assert projection["bandWidthInputs"]["sampleTier"] == expected_tier

    def test_smaller_sample_yields_a_strictly_wider_band(self):
        # Populations chosen so every variant still clears the instance floor and
        # therefore still carries a band — this test is about width, not about
        # the direction gate.
        widths = [
            width_of(band_of(seeded_finding(total_cases=p, owner_changes=p / 2)))
            for p in (800.0, 40.0, 12.0, 8.0)
        ]
        assert widths == sorted(widths), (
            f"band must widen monotonically as the sample thins: {widths}"
        )
        assert widths[0] < widths[-1]


class TestRecurrenceStabilityAxis:
    @pytest.mark.parametrize(
        "series, expected",
        [
            (STEADY_SERIES, STABILITY_STEADY),
            (VARIABLE_SERIES, STABILITY_VARIABLE),
            (BURSTY_SERIES, STABILITY_BURSTY),
            ([], STABILITY_UNKNOWN),
        ],
    )
    def test_stability_classifies_as_documented(self, series, expected):
        projection = band_of(seeded_finding(recent_values=series))
        assert projection["bandWidthInputs"]["recurrenceStability"] == expected

    def test_bursty_recurrence_yields_a_wider_band_than_steady(self):
        steady = band_of(seeded_finding(recent_values=STEADY_SERIES))
        variable = band_of(seeded_finding(recent_values=VARIABLE_SERIES))
        bursty = band_of(seeded_finding(recent_values=BURSTY_SERIES))
        assert width_of(steady) < width_of(variable) < width_of(bursty)

    def test_absent_history_is_wider_than_steady_never_narrower(self):
        """Absent history must never be flattered into 'steady'."""
        steady = band_of(seeded_finding(recent_values=STEADY_SERIES))
        unknown = band_of(seeded_finding(recent_values=[]))
        assert width_of(unknown) > width_of(steady)


class TestCorroborationAxis:
    def test_stronger_corroboration_yields_a_strictly_narrower_band(self):
        triple = band_of(seeded_finding(triple=True))
        corroborated = band_of(seeded_finding())
        supporting = band_of(
            seeded_finding(
                corroboration_sources=("Slack (supporting only)",),
                corroboration_rule_ids=("COR-05",),
            )
        )
        single = band_of(
            seeded_finding(
                corroboration_sources=(), corroboration_rule_ids=("COR-08",)
            )
        )
        widths = [
            width_of(triple),
            width_of(corroborated),
            width_of(supporting),
            width_of(single),
        ]
        assert widths == sorted(widths), (
            f"band must widen as corroboration weakens: {widths}"
        )
        assert width_of(triple) < width_of(single)

    def test_a_finding_stamped_both_cor05_and_cor08_reads_as_single_source(self):
        """COR-08 is the weaker state and must win — never flattered upward."""
        projection = band_of(
            seeded_finding(
                corroboration_sources=(), corroboration_rule_ids=("COR-05", "COR-08")
            )
        )
        assert (
            projection["bandWidthInputs"]["corroborationStatus"]
            == CORROBORATION_SINGLE_SOURCE
        )

    def test_legacy_record_without_corroboration_fields_is_not_flattered(self):
        finding = seeded_finding()
        finding.pop("corroboration_sources")
        finding.pop("corroboration_rule_ids")
        finding.pop("triple_corroboration")
        projection = band_of(finding)
        assert (
            projection["bandWidthInputs"]["corroborationStatus"]
            == CORROBORATION_SINGLE_SOURCE
        )
        assert projection["confidenceCapped"] is True


class TestConfidenceCapAxis:
    def test_confidence_cap_is_a_band_width_input_in_its_own_right(self):
        """LOW confidence widens the band even when corroboration is intact."""
        corroborated = band_of(seeded_finding(confidence="HIGH"))
        low = band_of(seeded_finding(confidence="LOW"))
        assert corroborated["bandWidthInputs"]["confidenceCapped"] is False
        assert low["bandWidthInputs"]["confidenceCapped"] is True
        assert low["bandWidthInputs"]["corroborationStatus"] == (
            corroborated["bandWidthInputs"]["corroborationStatus"]
        ), "the two findings must differ ONLY on the cap axis"
        assert width_of(low) > width_of(corroborated)

    def test_the_cap_axis_charges_the_documented_amount(self):
        uncapped = compute_band_width(inputs(capped=False))
        capped = compute_band_width(inputs(capped=True))
        assert capped.evidence_penalty - uncapped.evidence_penalty == pytest.approx(
            AXIS_WEIGHTS[AXIS_CONFIDENCE_CAP]
        )


class TestAxisWeights:
    def test_the_four_axis_weights_sum_to_one(self):
        """What bounds the widening — a fifth axis must not be smuggled in."""
        assert sum(AXIS_WEIGHTS.values()) == pytest.approx(1.0)
        assert set(AXIS_WEIGHTS) == {
            AXIS_SAMPLE_SIZE,
            AXIS_RECURRENCE_STABILITY,
            AXIS_CORROBORATION,
            AXIS_CONFIDENCE_CAP,
        }

    def test_best_and_worst_evidence_hit_the_documented_bounds(self):
        best = compute_band_width(
            inputs(
                sample_size=5000.0,
                stability=STABILITY_STEADY,
                corroboration=CORROBORATION_TRIPLE,
                capped=False,
            )
        )
        worst = compute_band_width(
            inputs(
                sample_size=None,
                stability=STABILITY_BURSTY,
                corroboration=CORROBORATION_SINGLE_SOURCE,
                capped=True,
            )
        )
        assert best.evidence_penalty == pytest.approx(0.0)
        assert worst.evidence_penalty == pytest.approx(1.0)
        assert best.width_pct < worst.width_pct
        assert best.low_pct < best.high_pct and worst.low_pct < worst.high_pct

    def test_every_driver_is_reported_with_its_contribution(self):
        """The width is auditable per axis, not an opaque number."""
        band_width = band_of(
            seeded_finding(
                total_cases=12.0,
                owner_changes=6.0,
                recent_values=BURSTY_SERIES,
                corroboration_sources=(),
                corroboration_rule_ids=("COR-08",),
            )
        )["bandWidth"]
        axes = [d["axis"] for d in band_width["drivers"]]
        assert axes == [
            AXIS_SAMPLE_SIZE,
            AXIS_RECURRENCE_STABILITY,
            AXIS_CORROBORATION,
            AXIS_CONFIDENCE_CAP,
        ]
        for driver in band_width["drivers"]:
            assert driver["penalty"] > 0, f"{driver['axis']} should be penalised here"
            assert driver["widensByPct"] > 0
            assert driver["label"] and driver["value"]


class TestNoManualConfiguration:
    def test_band_width_takes_no_override_and_reads_no_environment(self, monkeypatch):
        """AC2's "not a hand-set number", enforced structurally.

        A band must not be adjustable per deployment or per demo, so nothing in
        the model may read the environment.
        """
        import discovery.projection.band_width as module

        source = module.__doc__ or ""
        assert "never from a hand-set number" in source

        monkeypatch.setenv("BAND_WIDTH", "0.01")
        monkeypatch.setenv("PROJECTION_BAND_WIDTH", "0.01")
        monkeypatch.setenv("BAND_HALF_WIDTH", "0.01")
        assert band_of(seeded_finding())["bandWidth"] == compute_band_width(
            inputs(sample_size=800.0)
        ).to_dict()

    def test_module_never_imports_os_or_a_config_surface(self):
        """No env var, no config file, no operator knob — by construction."""
        from pathlib import Path

        import discovery.projection.band_width as module

        source = Path(module.__file__).read_text(encoding="utf-8")
        for forbidden in ("import os", "os.environ", "os.getenv", "load_config"):
            assert forbidden not in source, (
                f"band width must not be configurable: found {forbidden!r}"
            )


# --------------------------------------------------------------------------
# AC4 — capped confidence is labelled and never out-ranks a corroborated peer.
# --------------------------------------------------------------------------


class TestCappedConfidence:
    def _pair(self):
        """A corroborated finding and its capped twin — identical otherwise."""
        corroborated = band_of(seeded_finding())
        capped = band_of(
            seeded_finding(
                corroboration_sources=(), corroboration_rule_ids=("COR-08",)
            )
        )
        return corroborated, capped

    def test_capped_projection_is_labelled_as_such(self):
        _, capped = self._pair()
        assert capped["confidenceCapped"] is True
        assert capped["projectionStrength"]["capped"] is True
        assert capped["projectionStrength"]["cappedLabel"] == CAPPED_STRENGTH_LABEL
        assert capped["projectionStrength"]["label"] == CAPPED_STRENGTH_LABEL
        assert capped["bandWidth"]["confidenceCapped"] is True
        assert "single-source" in capped["projectionStrength"]["cappedLabel"].lower()

    def test_capped_band_is_wider_than_the_corroborated_equivalent(self):
        corroborated, capped = self._pair()
        assert width_of(capped) > width_of(corroborated)
        assert capped["bandWidth"]["evidenceQuality"] < corroborated["bandWidth"][
            "evidenceQuality"
        ]

    def test_capped_strength_is_clamped_below_the_ceiling(self):
        _, capped = self._pair()
        assert capped["projectionStrength"]["value"] <= CAPPED_STRENGTH_CEILING

    def test_a_huge_sample_never_lifts_a_capped_finding_past_a_corroborated_one(self):
        """AC4's real teeth: strength alone must not promote a capped finding.

        The capped finding here has a far LARGER sample than its corroborated
        rival, which is exactly the case where a naive strength scalar would
        rank it first.
        """
        corroborated = band_of(seeded_finding(total_cases=32.0, owner_changes=16.0))
        capped = band_of(
            seeded_finding(
                total_cases=5000.0,
                owner_changes=2500.0,
                corroboration_sources=(),
                corroboration_rule_ids=("COR-08",),
            )
        )
        assert projection_rank_key(corroborated) < projection_rank_key(capped)
        ordered = order_by_projection_strength(
            [capped, corroborated], projection_of=lambda p: p
        )
        assert ordered[0] is corroborated

    def test_ordering_is_stable_for_equally_capped_findings(self):
        first = band_of(
            seeded_finding(corroboration_sources=(), corroboration_rule_ids=("COR-08",))
        )
        second = band_of(
            seeded_finding(corroboration_sources=(), corroboration_rule_ids=("COR-08",))
        )
        items = [first, second]
        assert order_by_projection_strength(items, projection_of=lambda p: p) == items

    def test_demote_capped_preserves_every_other_ordering_decision(self):
        """The conservative rule the roadmap uses: demote only, re-rank never."""
        weak_uncapped = {"id": "weak", "projection": band_of(
            seeded_finding(total_cases=12.0, owner_changes=6.0)
        )}
        strong_uncapped = {"id": "strong", "projection": band_of(seeded_finding())}
        capped = {"id": "capped", "projection": band_of(
            seeded_finding(
                total_cases=5000.0,
                owner_changes=2500.0,
                corroboration_sources=(),
                corroboration_rule_ids=("COR-08",),
            )
        )}

        ordered = demote_capped_projections(
            [weak_uncapped, capped, strong_uncapped], lambda o: o["projection"]
        )
        assert [o["id"] for o in ordered] == ["weak", "strong", "capped"], (
            "capped sinks below uncapped; the weak-before-strong order the "
            "caller supplied is preserved"
        )

    def test_an_item_without_a_projection_is_treated_as_uncapped(self):
        capped = {"id": "capped", "projection": band_of(
            seeded_finding(corroboration_sources=(), corroboration_rule_ids=("COR-08",))
        )}
        no_projection = {"id": "none", "projection": None}
        ordered = demote_capped_projections(
            [capped, no_projection], lambda o: o["projection"]
        )
        assert [o["id"] for o in ordered] == ["none", "capped"]

    def test_a_pre_t4_stored_projection_is_still_recognised_as_capped(self):
        """Back-compat: the top-level flag alone is enough to demote."""
        legacy = {"confidenceCapped": True}
        assert projection_is_capped(legacy) is True
        assert projection_is_capped({"confidenceCapped": False}) is False
        assert projection_is_capped(None) is False


class TestProjectionStrength:
    def test_strength_tracks_evidence_quality_when_uncapped(self):
        strong = band_of(seeded_finding(triple=True))
        weak = band_of(
            seeded_finding(
                total_cases=12.0, owner_changes=6.0, recent_values=BURSTY_SERIES
            )
        )
        assert projection_strength_of(strong) > projection_strength_of(weak)
        assert strong["projectionStrength"]["tier"] == "strong"

    def test_a_finding_with_no_band_carries_no_strength(self):
        """No band, no comparable strength — never a zero that sorts as real."""
        too_thin = build_projection(
            seeded_finding(total_cases=2.0, owner_changes=1.0)
        )
        assert too_thin["direction"] == "no_material_change"
        assert too_thin["magnitudeBand"] is None
        assert too_thin["bandWidth"] is None
        assert too_thin["projectionStrength"]["value"] is None
        assert too_thin["projectionStrength"]["label"]

    def test_a_bandless_projection_sorts_below_a_banded_one(self):
        banded = band_of(seeded_finding())
        bandless = build_projection(seeded_finding(total_cases=2.0, owner_changes=1.0))
        ordered = order_by_projection_strength(
            [bandless, banded], projection_of=lambda p: p
        )
        assert ordered[0] is banded


# --------------------------------------------------------------------------
# Labels — the band and its evidence label must agree with each other.
# --------------------------------------------------------------------------


class TestLabels:
    def test_band_tier_is_read_from_the_computed_width_not_the_inputs(self):
        for finding in (
            seeded_finding(triple=True),
            seeded_finding(),
            seeded_finding(
                corroboration_sources=(), corroboration_rule_ids=("COR-08",)
            ),
            seeded_finding(
                total_cases=5.0, owner_changes=2.0, recent_values=BURSTY_SERIES
            ),
        ):
            projection = build_projection(finding)
            if projection["magnitudeBand"] is None:
                continue
            band_width = projection["bandWidth"]
            assert band_width["widthPct"] == width_of(projection)
            assert band_width["lowPct"] == projection["magnitudeBand"]["lowPct"]
            assert band_width["highPct"] == projection["magnitudeBand"]["highPct"]

    def test_evidence_label_and_thin_flag_reach_the_basis_block(self):
        """Opportunity Review reads the label off the basis it already renders."""
        projection = band_of(seeded_finding())
        basis = projection["basis"]
        assert basis["evidenceLabel"] == projection["bandWidth"]["evidenceLabel"]
        assert basis["evidenceTier"] == projection["bandWidth"]["evidenceTier"]
        assert basis["bandLabel"] == projection["bandWidth"]["bandLabel"]
        assert basis["bandTier"] == projection["bandWidth"]["bandTier"]
        assert basis["bandWidthRationale"] == projection["bandWidth"]["rationale"]
        assert basis["thinEvidence"] is projection["bandWidth"]["thinEvidence"]

    def test_thin_evidence_flag_is_set_whenever_any_axis_is_weak(self):
        assert band_of(seeded_finding())["bandWidth"]["thinEvidence"] is False
        for weak in (
            seeded_finding(total_cases=12.0, owner_changes=6.0),
            seeded_finding(recent_values=BURSTY_SERIES),
            seeded_finding(
                corroboration_sources=(), corroboration_rule_ids=("COR-08",)
            ),
            seeded_finding(confidence="LOW"),
        ):
            assert band_of(weak)["bandWidth"]["thinEvidence"] is True

    def test_rationale_names_the_inputs_that_widened_the_band(self):
        rationale = band_of(
            seeded_finding(
                total_cases=12.0,
                owner_changes=6.0,
                recent_values=BURSTY_SERIES,
                corroboration_sources=(),
                corroboration_rule_ids=("COR-08",),
            )
        )["bandWidth"]["rationale"]
        for expected in ("thin", "bursty", "single source", "capped confidence"):
            assert expected in rationale.lower(), rationale


# --------------------------------------------------------------------------
# AC3 — nothing this model emits is a point estimate or a savings claim.
# --------------------------------------------------------------------------

_FORBIDDEN_VOCABULARY = (
    "will save",
    "will reduce",
    "will cut",
    "guarantee",
    "guaranteed",
    "roi",
    "savings",
    "save ",
    "eliminates",
    "ensures",
)


def _all_strings(value):
    """Every string anywhere in a nested structure."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _all_strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _all_strings(item)


class TestVocabulary:
    @pytest.mark.parametrize(
        "finding",
        [
            seeded_finding(),
            seeded_finding(triple=True),
            seeded_finding(confidence="LOW"),
            seeded_finding(
                corroboration_sources=(), corroboration_rule_ids=("COR-08",)
            ),
            seeded_finding(
                total_cases=5.0, owner_changes=2.0, recent_values=BURSTY_SERIES
            ),
        ],
    )
    def test_no_savings_or_guarantee_language_anywhere_in_the_projection(
        self, finding
    ):
        """A whole-payload sweep, not just the fields T1-T3 already checked."""
        projection = build_projection(finding)
        for text in _all_strings(projection):
            lowered = text.lower()
            for phrase in _FORBIDDEN_VOCABULARY:
                assert phrase not in lowered, (
                    f"{text!r} contains forbidden phrase {phrase!r} — a band is "
                    "an evidence statement, not a savings claim"
                )

    def test_a_band_is_never_a_point_estimate_at_any_evidence_level(self):
        for population in (5000.0, 800.0, 40.0, 12.0, 4.0):
            projection = build_projection(
                seeded_finding(total_cases=population, owner_changes=population / 2)
            )
            band = projection["magnitudeBand"]
            if band is None:
                continue
            assert band["lowPct"] < band["highPct"], population
            assert 0 < band["lowPct"] and band["highPct"] <= 90
