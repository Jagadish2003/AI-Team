"""2.0-A1 T5 — the projection vocabulary guard and intervention-language copy.

AC3: *"No projection output — API, UI, report, or export — contains a
point-estimate savings claim or guarantee language; template-level check over the
projection vocabulary."*

These tests pin both halves of that:

  * the guard catches what it must catch — guarantee language and point-estimate
    savings claims, in the shapes a model or a template actually produces;
  * the guard does NOT catch what it must not — a magnitude band, a measured
    observation, or a score. A guard that flags the evidence is worse than no
    guard, because people learn to ignore it;
  * every recommendation this repo generates carries the five required parts and
    is clean by construction, for every profiled detector.

Pure unit tests: no DB, no clock, no network.
"""

from __future__ import annotations

import copy

import pytest

from discovery.projection import build_projection, get_detector_profile, known_detector_ids
from discovery.projection.recommendation import (
    PART_AGENT_HANDLES,
    PART_BAND_AND_HORIZON,
    PART_CASES_IN_SCOPE,
    PART_REMAINS_MANUAL,
    PART_SIGNAL_TO_MOVE,
    REQUIRED_PARTS,
    build_recommendation,
)
from discovery.projection.vocabulary import (
    CATEGORY_GUARANTEE,
    CATEGORY_POINT_ESTIMATE,
    ProhibitedVocabularyError,
    assert_clean,
    contains_prohibited,
    sanitize_bullets,
    sanitize_text,
    scan_payload,
    scan_text,
)


# --------------------------------------------------------------------------
# Seeded findings
# --------------------------------------------------------------------------


def seeded_finding(detector_id="HANDOFF_FRICTION", **overrides):
    finding = {
        "id": "opp_t5_001",
        "title": "Elevated case reassignment",
        "confidence": "HIGH",
        "impact": 8,
        "effort": 3,
        "tier": "Quick Win",
        "evidenceIds": ["ev_t5_001"],
        "corroboration_sources": ["ServiceNow", "Jira"],
        "corroboration_rule_ids": ["COR-01", "COR-02"],
        "triple_corroboration": False,
        "packId": "service_cloud",
        "packVersion": "1.2.0",
        "recent_values": [200.0, 205.0, 198.0, 202.0, 203.0],
        "baseline_mean": 201.6,
        "baseline_window_days": 90,
        "run_count": 5,
        "_debug": {
            "detector_id": detector_id,
            "metric_value": 2.4,
            "roadmap_stage": "NEXT_30",
            "raw_evidence": {
                "owner_changes_90d": 240.0,
                "total_cases_90d": 800.0,
                "handoff_score": 2.4,
            },
        },
    }
    finding.update(overrides)
    return finding


def recommendation_of(finding=None):
    projection = build_projection(finding or seeded_finding())
    assert projection is not None
    recommendation = projection["recommendation"]
    assert recommendation is not None
    return recommendation


# --------------------------------------------------------------------------
# The guard catches what it must catch.
# --------------------------------------------------------------------------


class TestProhibitedVocabularyIsCaught:
    @pytest.mark.parametrize(
        "text",
        [
            # The story's own example of what must never ship.
            "This agent will reduce cost by 40%.",
            "The agent will save 12 hours per week.",
            "This will cut handling time.",
            "Deployment guarantees a faster queue.",
            "Guaranteed reduction in reassignment.",
            "The agent eliminates repetitive escalation cycles.",
            "A sync agent would eliminate the duplicate records.",
            "This ensures no case is ever mis-routed.",
            "Estimated annual savings of $120,000.",
            "Cost savings across the service desk.",
            "ROI within six months.",
            "Payback period of four months.",
            "40% reduction in handle time.",
            "30% faster resolution.",
            "12 hours saved per week.",
            "Reduces effort by 3 FTE.",
            "It will completely remove the backlog.",
            "A risk-free deployment.",
        ],
    )
    def test_guarantee_and_point_estimate_claims_are_flagged(self, text):
        violations = scan_text(text)
        assert violations, f"guard missed prohibited copy: {text!r}"
        assert contains_prohibited(text)

    def test_violations_name_their_category(self):
        assert scan_text("The agent guarantees results.")[0].category == CATEGORY_GUARANTEE
        assert (
            scan_text("40% reduction in handle time.")[0].category
            == CATEGORY_POINT_ESTIMATE
        )

    def test_scan_is_case_insensitive(self):
        assert contains_prohibited("THIS WILL REDUCE COST BY 40%")
        assert contains_prohibited("Guaranteed Savings")


# --------------------------------------------------------------------------
# The guard does NOT catch what it must not.
# --------------------------------------------------------------------------


class TestLegitimateCopyIsNotFlagged:
    @pytest.mark.parametrize(
        "text",
        [
            # A BAND is exactly what the platform is supposed to say.
            "23-57% of the recurring instances.",
            "Projected movement is a band of 25–55% of the recurring instances.",
            "Between 13% and 67% of the observed delay.",
            "Projected 25 to 55% of the observed rate.",
            # Measured observations — the evidence story depends on these.
            "240 owner changes recorded across 800 Cases in 90 days.",
            "Impact 8/10, Effort 3/10.",
            "Reassignment rate 2.4 per case against a threshold of 1.5.",
            "45 pending approvals with an average delay of 12.5 days.",
            "currently 120 days; a lower value is the expected direction of movement",
            # Intervention language — the wording the story asks for.
            "Agent handles the 240 recurring cases; the residual requires judgement.",
            "The agent takes over manually re-routing cases between queues.",
            "Remaining manual: cases whose correct owner is genuinely ambiguous.",
        ],
    )
    def test_bands_measurements_and_intervention_language_pass(self, text):
        violations = scan_text(text)
        assert not violations, (
            f"guard wrongly flagged legitimate copy {text!r}: "
            + "; ".join(str(v) for v in violations)
        )

    def test_a_band_is_never_read_as_a_point_estimate(self):
        """The single most important false-positive to avoid.

        A range and a point estimate look alike to a naive regex; if the guard
        flagged bands, the honest output would be the thing it suppressed.
        """
        assert not contains_prohibited("a band of 23-57% of the recurring instances")
        assert contains_prohibited("a 40% reduction in the recurring instances")

    def test_machine_identifiers_are_skipped_when_sweeping_a_payload(self):
        """A detector id is not customer-facing copy."""
        payload = {"detectorId": "COST_REDUCTION_GAP", "signalName": "hours_saved_90d"}
        assert scan_payload(payload) == []


# --------------------------------------------------------------------------
# Enforcement behaviour.
# --------------------------------------------------------------------------


class TestSanitisation:
    def test_only_the_offending_sentence_is_removed(self):
        text = (
            "Cases are re-routed twice on average. This agent will reduce cost by 40%. "
            "The pattern is corroborated by ServiceNow."
        )
        cleaned = sanitize_text(text)
        assert "will reduce cost" not in cleaned
        assert "re-routed twice on average" in cleaned
        assert "corroborated by ServiceNow" in cleaned

    def test_clean_text_is_returned_untouched(self):
        text = "Agent handles the 240 recurring cases; the residual requires judgement."
        assert sanitize_text(text) == text

    def test_wholly_prohibited_text_becomes_an_explicit_notice(self):
        """Never a silent empty string that reads as 'nothing to say'."""
        cleaned = sanitize_text("This agent will reduce cost by 40%.")
        assert cleaned
        assert not contains_prohibited(cleaned)
        assert "removed" in cleaned.lower()

    def test_bullets_carrying_a_claim_are_dropped_whole(self):
        bullets = [
            "240 owner changes recorded in 90 days.",
            "This will save 12 hours per week.",
            "Corroborated by ServiceNow incidents.",
        ]
        kept = sanitize_bullets(bullets)
        assert kept == [
            "240 owner changes recorded in 90 days.",
            "Corroborated by ServiceNow incidents.",
        ]

    def test_assert_clean_raises_on_our_own_copy(self):
        with pytest.raises(ProhibitedVocabularyError) as excinfo:
            assert_clean("This agent will reduce cost by 40%.", "template.x")
        assert "template.x" in str(excinfo.value)

    def test_assert_clean_passes_intervention_language(self):
        assert_clean(
            "Agent handles the 240 recurring cases; the residual requires judgement.",
            "template.ok",
        )


# --------------------------------------------------------------------------
# The recommendation itself.
# --------------------------------------------------------------------------


class TestRecommendationShape:
    def test_recommendation_carries_all_five_required_parts(self):
        recommendation = recommendation_of()
        assert [p["id"] for p in recommendation["parts"]] == list(REQUIRED_PARTS)
        for part in recommendation["parts"]:
            assert part["label"] and part["text"], part

    def test_headline_is_intervention_shaped(self):
        headline = recommendation_of()["headline"]
        assert headline.startswith("Agent handles ")
        assert "residual requires judgement" in headline
        assert "240" in headline, "the headline must name the N recurring cases"

    def test_each_part_says_the_thing_it_is_named_for(self):
        parts = {p["id"]: p["text"] for p in recommendation_of()["parts"]}

        # 1. what the agent handles — the manual step it takes over
        assert "takes over" in parts[PART_AGENT_HANDLES]
        assert "re-routing cases" in parts[PART_AGENT_HANDLES]
        # 2. which recurring cases are in scope — the measured N
        assert "In scope:" in parts[PART_CASES_IN_SCOPE]
        assert "240" in parts[PART_CASES_IN_SCOPE]
        # 3. what remains manual
        assert parts[PART_REMAINS_MANUAL].startswith("Remaining manual:")
        # 4. which measured signal is expected to move — named, real field
        assert "owner_changes_90d" in parts[PART_SIGNAL_TO_MOVE]
        # 5. the band and horizon
        assert "band of" in parts[PART_BAND_AND_HORIZON]
        assert "days" in parts[PART_BAND_AND_HORIZON]

    def test_next_steps_are_actions_not_outcomes(self):
        steps = recommendation_of()["nextSteps"]
        assert steps
        for step in steps:
            assert not contains_prohibited(step), step
        assert any("baseline" in s for s in steps), (
            "a next step must set up the re-measurement 2.0-A2 depends on"
        )

    def test_capped_confidence_is_stated_in_the_band_part(self):
        capped = recommendation_of(
            seeded_finding(corroboration_sources=[], corroboration_rule_ids=["COR-08"])
        )
        band_part = next(
            p for p in capped["parts"] if p["id"] == PART_BAND_AND_HORIZON
        )
        assert "capped" in band_part["text"].lower()

    def test_a_finding_with_no_band_still_recommends_honestly(self):
        finding = seeded_finding()
        finding["_debug"]["raw_evidence"] = {
            "owner_changes_90d": 1.0,
            "total_cases_90d": 2.0,
        }
        projection = build_projection(finding)
        assert projection["magnitudeBand"] is None
        band_part = next(
            p
            for p in projection["recommendation"]["parts"]
            if p["id"] == PART_BAND_AND_HORIZON
        )
        assert "No magnitude band is projected" in band_part["text"]

    def test_recommendation_is_none_without_a_projection(self):
        assert build_recommendation(seeded_finding(), None) is None
        assert build_recommendation(seeded_finding(), {}) is None

    def test_recommendation_is_deterministic(self):
        finding = seeded_finding()
        first = build_projection(copy.deepcopy(finding))["recommendation"]
        for _ in range(3):
            assert build_projection(copy.deepcopy(finding))["recommendation"] == first


class TestEveryDetectorProducesCleanCopy:
    """The template-level check AC3 asks for, run over every profiled detector."""

    @pytest.mark.parametrize("detector_id", sorted(known_detector_ids()))
    def test_projection_payload_carries_no_prohibited_vocabulary(self, detector_id):
        profile = get_detector_profile(detector_id)
        raw = {profile.movement_signal: 50.0}
        if profile.instance_field:
            raw[profile.instance_field] = 40.0
        if profile.volume_signal:
            raw[profile.volume_signal] = 200.0

        finding = seeded_finding(detector_id)
        finding["_debug"]["raw_evidence"] = raw
        finding["_debug"]["metric_value"] = 50.0

        projection = build_projection(finding)
        assert projection is not None, detector_id

        violations = scan_payload(projection)
        assert not violations, (
            f"{detector_id} projection carries prohibited vocabulary: "
            + "; ".join(str(v) for v in violations)
        )

    @pytest.mark.parametrize("detector_id", sorted(known_detector_ids()))
    def test_every_detector_declares_a_residual(self, detector_id):
        """An agent that leaves nothing to judgement is a claim we do not make."""
        profile = get_detector_profile(detector_id)
        assert profile.residual.strip(), detector_id
        assert profile.case_noun.strip(), detector_id
        assert_clean(profile.residual, f"{detector_id}.residual")
        assert_clean(profile.case_noun, f"{detector_id}.case_noun")
        assert_clean(profile.manual_step, f"{detector_id}.manual_step")


class TestStaticTemplatesAreClean:
    """Our OWN copy must be clean by construction, not sanitised at runtime."""

    def test_track_a_rationale_templates_carry_no_claims(self):
        from discovery.track_a_adapter import _DETECTOR_META

        for detector_id, meta in _DETECTOR_META.items():
            for field in ("title_template", "rationale_template"):
                assert_clean(meta.get(field, ""), f"track_a.{detector_id}.{field}")

    def test_blueprint_detector_meta_carries_no_claims(self):
        from app.routes_sprint41_blueprint import _DETECTOR_META, _FALLBACK_META

        violations = scan_payload(_DETECTOR_META) + scan_payload(_FALLBACK_META)
        assert not violations, "; ".join(str(v) for v in violations)

    def test_llm_prompts_do_not_ask_for_savings_language(self):
        """The prompt must not request the very thing the guard strips."""
        from pathlib import Path

        import app.llm_enrichment as module

        source = Path(module.__file__).read_text(encoding="utf-8")
        for phrase in ('"could reduce"', "estimated savings", "projected savings"):
            assert phrase not in source, (
                f"llm_enrichment prompt still asks for {phrase!r}"
            )
        assert "NEVER state a saving" in source, (
            "the prompt must explicitly forbid savings language"
        )
