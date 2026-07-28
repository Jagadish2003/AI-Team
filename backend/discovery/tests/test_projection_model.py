"""2.0-A1 T1 — intervention projection model tests.

Covers the contract the story states:
  * every projection carries direction, band, horizon, manual step, movement
    signal, its assumption ledger, and its computation basis (AC1);
  * band width is deterministic from sample size, recurrence stability, and
    corroboration status, and thinner evidence yields a demonstrably WIDER band
    (AC2);
  * a capped (single-source) finding is labelled as such and never bands
    narrower than a corroborated equivalent (AC4);
  * re-running against unchanged signal reproduces identical output (AC5).

Pure unit tests — the projection model touches no DB and no clock.
"""

from __future__ import annotations

import copy
import re

import pytest

from discovery.projection import (
    DIRECTION_IMPROVES,
    DIRECTION_NO_MATERIAL_CHANGE,
    build_projection,
    project_opportunities,
)
from discovery.projection.signal_registry import (
    get_detector_profile,
    known_detector_ids,
)


# --------------------------------------------------------------------------
# Fixtures — opportunity records shaped exactly as the Track A adapter stores
# them (numeric raw_evidence under _debug).
# --------------------------------------------------------------------------


def _opp(
    detector_id="HANDOFF_FRICTION",
    raw_evidence=None,
    confidence="HIGH",
    corroboration_sources=None,
    corroboration_rule_ids=None,
    triple=False,
    recent_values=None,
    roadmap_stage="NEXT_30",
    **extra,
):
    """A stored-shape opportunity. Defaults are the STRONG-evidence case."""
    if raw_evidence is None:
        raw_evidence = {
            "owner_changes_90d": 240.0,
            "total_cases_90d": 800.0,
            "handoff_score": 2.4,
        }
    opp = {
        "id": "opp_001",
        "title": "Elevated case reassignment",
        "confidence": confidence,
        "impact": 8,
        "effort": 3,
        "tier": "Quick Win",
        "evidenceIds": ["ev_sf_aaa111"],
        "corroboration_sources": (
            ["ServiceNow", "Jira"]
            if corroboration_sources is None
            else corroboration_sources
        ),
        "corroboration_rule_ids": (
            ["COR-01", "COR-02"]
            if corroboration_rule_ids is None
            else corroboration_rule_ids
        ),
        "triple_corroboration": triple,
        "packId": "service_cloud",
        "packVersion": "1.2.0",
        "_debug": {
            "detector_id": detector_id,
            "signal_source": "salesforce",
            "metric_value": 2.4,
            "threshold": 1.5,
            "roadmap_stage": roadmap_stage,
            "raw_evidence": dict(raw_evidence),
        },
    }
    # Temporal fields land on the opp before projection runs.
    if recent_values is not None:
        opp["recent_values"] = list(recent_values)
        opp["baseline_mean"] = sum(recent_values) / len(recent_values)
        opp["baseline_stddev"] = 4.0
        opp["baseline_window_days"] = 90
        opp["run_count"] = len(recent_values)
        opp["signal_key"] = "service_cloud::HANDOFF_FRICTION::metric_value"
    opp.update(extra)
    return opp


def _steady():
    return [200.0, 205.0, 198.0, 202.0, 203.0]


def _bursty():
    return [10.0, 400.0, 25.0, 380.0, 15.0]


def _band_width(projection):
    band = projection["magnitudeBand"]
    return band["highPct"] - band["lowPct"]


# --------------------------------------------------------------------------
# AC1 — every projection carries every required part.
# --------------------------------------------------------------------------

_REQUIRED_TOP_LEVEL = (
    "schemaVersion",
    "direction",
    "magnitudeBand",
    "observationHorizonDays",
    "manualStepReplaced",
    "movementSignal",
    "assumptionLedger",
    "affectedSignals",
    "basis",
    "bandWidthInputs",
    "confidenceCapped",
)


def test_projection_carries_every_required_part():
    projection = build_projection(_opp(recent_values=_steady()))
    assert projection is not None
    for key in _REQUIRED_TOP_LEVEL:
        assert key in projection, f"projection missing required part: {key}"

    assert projection["direction"] == DIRECTION_IMPROVES
    band = projection["magnitudeBand"]
    assert band["lowPct"] < band["highPct"], "band must never be a point estimate"
    assert projection["observationHorizonDays"] in (30, 60, 90)
    assert projection["manualStepReplaced"]

    movement = projection["movementSignal"]
    for key in ("concept", "conceptLabel", "signalName", "unit", "currentValue"):
        assert key in movement

    assumptions = projection["assumptionLedger"]
    assert len(assumptions) == 5
    for assumption in assumptions:
        for key in ("id", "label", "description"):
            assert assumption[key], f"assumption missing {key}"

    basis = projection["basis"]
    for key in (
        "detectorId",
        "observedInstances",
        "observedPopulation",
        "instanceSignal",
        "confidence",
        "corroborationStatus",
        "evidenceIds",
    ):
        assert key in basis, f"basis missing: {key}"


def test_projection_carries_the_required_assumption_ledger():
    projection = build_projection(_opp(recent_values=_steady()))
    labels = [a["label"] for a in projection["assumptionLedger"]]

    assert labels == [
        "Agent handles the identified recurring cases",
        "Adoption is complete for those cases",
        "Upstream volume remains within its observed range",
        "Residual cases still require human judgement",
        "Projection applies only to the measured signal and horizon shown",
    ]
    assert any("manual step" in a["description"] for a in projection["assumptionLedger"])
    assert any("30-day observation horizon" in a["description"] for a in projection["assumptionLedger"])


def test_movement_signal_is_a_real_measured_field():
    """The projected signal must be a field the detector actually measures."""
    opp = _opp(recent_values=_steady())
    projection = build_projection(opp)
    raw = opp["_debug"]["raw_evidence"]

    movement = projection["movementSignal"]
    assert movement["signalName"] in raw
    assert movement["currentValue"] == raw[movement["signalName"]]

    for signal in projection["affectedSignals"]:
        assert signal["signalName"] in raw, (
            f"projected signal {signal['signalName']!r} is not a measured field"
        )


def test_basis_reports_observed_counts_and_window():
    projection = build_projection(_opp(recent_values=_steady()))
    basis = projection["basis"]
    assert basis["observedInstances"] == 240
    assert basis["observedPopulation"] == 800
    assert basis["baselineWindowDays"] == 90
    assert basis["observedRunCount"] == 5
    assert basis["packVersion"] == "1.2.0"


# --------------------------------------------------------------------------
# AC2 — band width is deterministic, and thin evidence bands WIDER.
# --------------------------------------------------------------------------


def test_thin_sample_yields_wider_band_than_strong_sample():
    strong = build_projection(
        _opp(
            raw_evidence={
                "owner_changes_90d": 240.0,
                "total_cases_90d": 800.0,
                "handoff_score": 2.4,
            },
            recent_values=_steady(),
        )
    )
    thin = build_projection(
        _opp(
            raw_evidence={
                "owner_changes_90d": 6.0,
                "total_cases_90d": 12.0,
                "handoff_score": 2.4,
            },
            recent_values=_steady(),
        )
    )
    assert _band_width(thin) > _band_width(strong)
    assert thin["bandWidthInputs"]["thinEvidence"] is True
    assert strong["bandWidthInputs"]["thinEvidence"] is False


def test_bursty_recurrence_yields_wider_band_than_steady():
    steady = build_projection(_opp(recent_values=_steady()))
    bursty = build_projection(_opp(recent_values=_bursty()))
    assert bursty["bandWidthInputs"]["recurrenceStability"] == "bursty"
    assert steady["bandWidthInputs"]["recurrenceStability"] == "steady"
    assert _band_width(bursty) > _band_width(steady)


def test_single_source_yields_wider_band_than_corroborated():
    corroborated = build_projection(_opp(recent_values=_steady()))
    single = build_projection(
        _opp(
            recent_values=_steady(),
            corroboration_sources=[],
            corroboration_rule_ids=[],
        )
    )
    assert single["bandWidthInputs"]["corroborationStatus"] == "single_source"
    assert _band_width(single) > _band_width(corroborated)


def test_triple_corroboration_yields_narrowest_band():
    triple = build_projection(
        _opp(recent_values=_steady(), triple=True)
    )
    corroborated = build_projection(_opp(recent_values=_steady()))
    assert triple["bandWidthInputs"]["corroborationStatus"] == "triple"
    assert _band_width(triple) < _band_width(corroborated)


def test_absent_history_bands_wider_than_steady_history():
    """Unknown stability must widen, not be assumed steady."""
    no_history = build_projection(_opp())
    steady = build_projection(_opp(recent_values=_steady()))
    assert no_history["bandWidthInputs"]["recurrenceStability"] == "unknown"
    assert _band_width(no_history) > _band_width(steady)


def test_band_never_collapses_to_a_point_and_stays_in_bounds():
    for stability in (_steady(), _bursty(), None):
        for sources in ([], ["ServiceNow", "Jira"]):
            projection = build_projection(
                _opp(recent_values=stability, corroboration_sources=sources)
            )
            band = projection["magnitudeBand"]
            assert 0 < band["lowPct"] < band["highPct"] <= 90


# --------------------------------------------------------------------------
# AC4 — capped (single-source) findings are labelled and never band narrower.
# --------------------------------------------------------------------------


def test_single_source_projection_is_labelled_capped():
    single = build_projection(
        _opp(
            recent_values=_steady(),
            confidence="MEDIUM",
            corroboration_sources=[],
            corroboration_rule_ids=["COR-08"],
        )
    )
    assert single["confidenceCapped"] is True
    # COR-08 means nothing corroborates the finding — it must not be flattered
    # into the stronger "supporting_only" state.
    assert single["basis"]["corroborationStatus"] == "single_source"


def test_single_source_rule_wins_over_conversation_rule():
    """COR-08 + COR-05 with no elevating source is still single-source."""
    projection = build_projection(
        _opp(
            recent_values=_steady(),
            corroboration_sources=["Slack (supporting only)"],
            corroboration_rule_ids=["COR-05", "COR-08"],
        )
    )
    assert projection["bandWidthInputs"]["corroborationStatus"] == "single_source"


def test_conversation_supporting_only_is_capped():
    """COR-05 (Slack supporting only) must not read as corroborated."""
    projection = build_projection(
        _opp(
            recent_values=_steady(),
            corroboration_sources=["Slack (supporting only)"],
            corroboration_rule_ids=["COR-05"],
        )
    )
    assert projection["bandWidthInputs"]["corroborationStatus"] == "supporting_only"
    assert projection["confidenceCapped"] is True


def test_capped_finding_never_bands_narrower_than_corroborated_equivalent():
    """AC4: identical evidence, only corroboration differs."""
    corroborated = build_projection(_opp(recent_values=_steady()))
    capped = build_projection(
        _opp(
            recent_values=_steady(),
            corroboration_sources=[],
            corroboration_rule_ids=["COR-08"],
        )
    )
    assert _band_width(capped) >= _band_width(corroborated)
    assert capped["magnitudeBand"]["highPct"] >= corroborated["magnitudeBand"]["highPct"]


def test_legacy_opportunity_without_corroboration_fields_reads_single_source():
    opp = _opp(recent_values=_steady())
    del opp["corroboration_sources"]
    del opp["corroboration_rule_ids"]
    del opp["triple_corroboration"]
    projection = build_projection(opp)
    assert projection["bandWidthInputs"]["corroborationStatus"] == "single_source"
    assert projection["confidenceCapped"] is True


# --------------------------------------------------------------------------
# AC5 — reproducibility.
# --------------------------------------------------------------------------


def test_identical_input_yields_identical_projection():
    opp = _opp(recent_values=_steady())
    first = build_projection(copy.deepcopy(opp))
    second = build_projection(copy.deepcopy(opp))
    assert first == second


def test_projection_does_not_mutate_the_opportunity_it_reads():
    opp = _opp(recent_values=_steady())
    before = copy.deepcopy(opp)
    build_projection(opp)
    assert opp == before


def test_every_profiled_detector_projects_reproducibly():
    """Each detector's own signal names produce a stable projection."""
    for detector_id in known_detector_ids():
        profile = get_detector_profile(detector_id)
        raw = {profile.movement_signal: 50.0, profile.instance_field: 40.0}
        if profile.volume_signal:
            raw[profile.volume_signal] = 200.0
        opp = _opp(
            detector_id=detector_id, raw_evidence=raw, recent_values=_steady()
        )
        first = build_projection(copy.deepcopy(opp))
        second = build_projection(copy.deepcopy(opp))
        assert first is not None, f"{detector_id} produced no projection"
        assert first == second, f"{detector_id} is not reproducible"
        assert first["manualStepReplaced"], f"{detector_id} has no manual step"
        assert first["movementSignal"]["signalName"] == profile.movement_signal


# --------------------------------------------------------------------------
# Direction, horizon, and the honest-silence cases.
# --------------------------------------------------------------------------


def test_too_few_instances_projects_no_material_change_with_no_band():
    projection = build_projection(
        _opp(
            raw_evidence={
                "owner_changes_90d": 1.0,
                "total_cases_90d": 2.0,
                "handoff_score": 2.4,
            },
            recent_values=_steady(),
        )
    )
    assert projection["direction"] == DIRECTION_NO_MATERIAL_CHANGE
    assert projection["magnitudeBand"] is None


def test_unmapped_detector_yields_no_projection():
    assert build_projection(_opp(detector_id="NOT_A_REAL_DETECTOR")) is None


# --- Rate-based detectors: a rate is never a sample size --------------------
#
# Regression: a real "42% of breaches concentrate in one team" finding projected
# "no material change" because the rate 0.42 was read as 0.42 observed instances
# and tripped the minimum-instance gate.


def test_rate_only_finding_still_projects_improvement():
    projection = build_projection(
        _opp(
            detector_id="ENT_SLA_BREACH_BY_TEAM",
            raw_evidence={"top_team_breach_rate": 0.42},
            recent_values=_steady(),
        )
    )
    assert projection["direction"] == DIRECTION_IMPROVES
    assert projection["magnitudeBand"] is not None, (
        "a fired rate-based detector must not be denied a projection just "
        "because its population is not counted"
    )


def test_rate_only_finding_bands_widest_on_unknown_sample():
    """Unknown population => minimal tier, not a false 'small sample'."""
    rate_only = build_projection(
        _opp(
            detector_id="ENT_SLA_BREACH_BY_TEAM",
            raw_evidence={"top_team_breach_rate": 0.42},
            recent_values=_steady(),
        )
    )
    with_population = build_projection(
        _opp(
            detector_id="ENT_SLA_BREACH_BY_TEAM",
            raw_evidence={"top_team_breach_rate": 0.42, "teams_analysed": 40},
            recent_values=_steady(),
        )
    )
    assert rate_only["bandWidthInputs"]["sampleTier"] == "minimal"
    assert _band_width(rate_only) > _band_width(with_population)


def test_rate_is_never_reported_as_an_instance_count():
    projection = build_projection(
        _opp(
            detector_id="GITHUB_COMMIT_CONCENTRATION",
            raw_evidence={"top_author_pct": 0.72, "total_contributors": 12},
            recent_values=_steady(),
        )
    )
    assert projection["basis"]["observedInstances"] is None
    assert projection["basis"]["observedPopulation"] == 12


def test_band_basis_unit_matches_what_the_signal_measures():
    """A band must not call itself a share of instances when it moves a rate."""
    cases = {
        "HANDOFF_FRICTION": "of the recurring instances",
        "GITHUB_PR_REVIEW_BOTTLENECK": "of the observed delay",
        "ENT_SLA_BREACH_BY_TEAM": "of the observed rate",
    }
    for detector_id, expected in cases.items():
        profile = get_detector_profile(detector_id)
        raw = {profile.movement_signal: 20.0}
        if profile.volume_signal:
            raw[profile.volume_signal] = 200.0
        if profile.instance_signal:
            raw[profile.instance_signal] = 40.0
        projection = build_projection(
            _opp(detector_id=detector_id, raw_evidence=raw, recent_values=_steady())
        )
        assert projection["magnitudeBand"]["basisUnit"] == expected, detector_id


def test_opportunity_with_no_detector_id_yields_no_projection():
    opp = _opp(recent_values=_steady())
    opp["_debug"]["detector_id"] = ""
    assert build_projection(opp) is None


def test_opportunity_with_no_measured_signals_yields_no_band():
    """No raw evidence => no honest sample size => no improvement claim."""
    opp = _opp(raw_evidence={})
    projection = build_projection(opp)
    assert projection is not None
    assert projection["direction"] == DIRECTION_NO_MATERIAL_CHANGE
    assert projection["magnitudeBand"] is None


@pytest.mark.parametrize(
    "stage,stability,expected",
    [
        ("NEXT_30", "steady", 30),
        ("NEXT_60", "steady", 60),
        ("NEXT_90", "steady", 90),
        ("NEXT_30", "bursty", 60),   # erratic signal needs a longer window
        ("NEXT_60", "bursty", 90),
    ],
)
def test_horizon_follows_stage_and_extends_when_erratic(stage, stability, expected):
    values = _steady() if stability == "steady" else _bursty()
    projection = build_projection(_opp(roadmap_stage=stage, recent_values=values))
    assert projection["observationHorizonDays"] == expected


def test_horizon_is_always_30_60_or_90():
    for stage in ("NEXT_30", "NEXT_60", "NEXT_90", "", "GARBAGE"):
        projection = build_projection(
            _opp(roadmap_stage=stage, recent_values=_steady())
        )
        assert projection["observationHorizonDays"] in (30, 60, 90)


# --------------------------------------------------------------------------
# AC3 groundwork — no point estimate, no guarantee language.
# 2.0-A1 T3 owns the full vocabulary guard; these pin what T1 generates.
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


def test_generated_text_uses_no_guarantee_or_savings_language():
    for detector_id in known_detector_ids():
        profile = get_detector_profile(detector_id)
        raw = {profile.movement_signal: 50.0, profile.instance_field: 40.0}
        if profile.volume_signal:
            raw[profile.volume_signal] = 200.0
        projection = build_projection(
            _opp(detector_id=detector_id, raw_evidence=raw, recent_values=_steady())
        )
        text = " ".join(
            [
                projection["manualStepReplaced"],
                projection["magnitudeBand"]["label"],
                projection["movementSignal"]["conceptLabel"],
                *[
                    assumption["label"]
                    for assumption in projection["assumptionLedger"]
                ],
                *[
                    assumption["description"]
                    for assumption in projection["assumptionLedger"]
                ],
            ]
        ).lower()
        for phrase in _FORBIDDEN_VOCABULARY:
            assert phrase not in text, (
                f"{detector_id} projection text contains forbidden phrase {phrase!r}"
            )


def test_band_label_is_a_range_never_a_point_estimate():
    projection = build_projection(_opp(recent_values=_steady()))
    label = projection["magnitudeBand"]["label"]
    assert re.match(r"^\d+–\d+% ", label), label
    low, high = (int(n) for n in re.findall(r"\d+", label)[:2])
    assert low < high


# --------------------------------------------------------------------------
# project_opportunities — the pipeline entry point.
# --------------------------------------------------------------------------


def test_project_opportunities_attaches_in_place_and_counts():
    opps = [
        _opp(recent_values=_steady()),
        _opp(detector_id="GITHUB_STALE_BRANCHES",
             raw_evidence={"stale_count": 40.0, "total_branches": 300.0,
                           "oldest_stale_days": 120.0},
             recent_values=_steady()),
    ]
    assert project_opportunities(opps) == 2
    assert all("projection" in o for o in opps)


def test_project_opportunities_never_raises_and_never_drops():
    """Non-blocking contract: bad input is skipped, good input still projected."""
    good = _opp(recent_values=_steady())
    opps = [
        {"id": "opp_bad", "_debug": None},          # unusable
        {"id": "opp_nodetector"},                    # no detector
        None,                                        # not a dict
        good,
    ]
    projected = project_opportunities(opps)
    assert projected == 1
    assert len(opps) == 4, "an opportunity must never be dropped"
    assert "projection" in good
    assert "projection" not in opps[1]


def test_project_opportunities_is_idempotent():
    opps = [_opp(recent_values=_steady())]
    project_opportunities(opps)
    first = copy.deepcopy(opps[0]["projection"])
    project_opportunities(opps)
    assert opps[0]["projection"] == first
