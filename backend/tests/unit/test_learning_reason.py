"""2.0-A3 T3 — the adjustment reason: structured data, and the sentence from it.

Pure unit tests. What is being protected, in order of importance:

* **the reason is DATA, and the sentence is rendered from it** — a prose string
  composed at the point of adjustment could not be counted, filtered or
  re-rendered, which is exactly what A2 T4 established for confounders;
* **links, not just counts** — every contributing decision and outcome carries
  an identifier that resolves;
* **the copy never overclaims** — including the subtle case where it would imply
  the learned signal contributed to the finding's credibility;
* **it is honest when the evidence is thin**.
"""

from __future__ import annotations

import pytest

from app.learning_adjustment import OpportunityAdjustment
from app.learning_reason import (
    CREDIBILITY_FIELDS,
    DIRECTION_DOWN,
    DIRECTION_UP,
    STRENGTH_LIMITED,
    STRENGTH_MINIMAL,
    STRENGTH_MODERATE,
    STRENGTH_SUBSTANTIAL,
    AdjustmentReason,
    build_reason,
    describe_adjustment,
    reason_placement_violations,
    render_reason,
)
from app.learning_reason_vocabulary import (
    CATEGORY_CREDIBILITY_IMPLICATION,
    CATEGORY_IMPORTANCE_CLAIM,
    CATEGORY_KNOWLEDGE_CLAIM,
    ProhibitedLearningCopyError,
    assert_clean,
    contains_prohibited,
    scan_payload,
    scan_text,
)


def decision_ref(index: int, action: str = "accept", **kw):
    ref = {
        "kind": "decision",
        "feedbackId": f"fb_{index:03d}",
        "action": action,
        "opportunityIdentity": f"ident_{index:03d}",
        "actorId": "analyst_1",
        "recordedAt": "2026-08-01T10:00:00+00:00",
    }
    ref.update(kw)
    return ref


def outcome_ref(verdict: str = "within_band", **kw):
    ref = {
        "kind": "outcome",
        "opportunityIdentity": "ident_out",
        "verdict": verdict,
        "currentRunId": "run_current",
        "baselineRunId": "run_baseline",
        "comparabilityVerdict": "comparable",
    }
    ref.update(kw)
    return ref


def adjustment(moved: int = -2, refs=(), *, capped=None, has_outcome=False):
    return OpportunityAdjustment(
        opportunity_id="opp_001",
        opportunity_identity="ident_001",
        detector_id="handoff_friction",
        pack_id="service_cloud",
        base_rank=5,
        adjusted_rank=5 + moved,
        base_impact=7.0,
        requested_delta=2.0,
        applied_delta=1.05,
        requested_rank_delta=2,
        net_weight=4.0,
        has_outcome_evidence=has_outcome,
        signal_count=len(refs),
        capped_by=capped,
        contributing_refs=tuple(refs),
    )


#: The story's own worked example.
STORY_CASE = adjustment(
    -2,
    [decision_ref(i) for i in range(4)] + [outcome_ref("within_band")],
    has_outcome=True,
)


# --------------------------------------------------------------------------
# The reason is structured data.
# --------------------------------------------------------------------------


class TestTheReasonIsStructuredData:
    def test_it_carries_counts_not_only_prose(self):
        """A2 T4's pattern: countable by a portfolio view, renderable by a UI."""
        reason = build_reason(STORY_CASE)
        assert reason.decision_count == 4
        assert reason.outcome_count == 1
        assert reason.decisions_by_action == {"accept": 4}
        assert reason.outcomes_by_verdict == {"within_band": 1}

    def test_it_carries_the_direction_and_magnitude(self):
        assert build_reason(adjustment(-2)).direction == DIRECTION_UP
        assert build_reason(adjustment(3)).direction == DIRECTION_DOWN
        assert build_reason(adjustment(-2)).ranks_moved == 2

    def test_it_carries_whether_the_cap_bound_it(self):
        capped = build_reason(adjustment(-3, capped="rank_move"))
        assert capped.was_capped
        assert capped.capped_by == "rank_move"
        assert not build_reason(adjustment(-1)).was_capped

    def test_the_breakdown_distinguishes_actions(self):
        reason = build_reason(
            adjustment(
                1,
                [
                    decision_ref(1, "dismiss"),
                    decision_ref(2, "dismiss"),
                    decision_ref(3, "defer"),
                ],
            )
        )
        assert reason.decisions_by_action == {"dismiss": 2, "defer": 1}

    def test_the_breakdown_distinguishes_verdicts(self):
        reason = build_reason(
            adjustment(
                -1,
                [outcome_ref("within_band"), outcome_ref("below_band")],
                has_outcome=True,
            )
        )
        assert reason.outcomes_by_verdict == {"within_band": 1, "below_band": 1}

    def test_a_finding_that_did_not_move_has_no_reason(self):
        """A reason with nothing to explain is noise.

        Rendering "this did not move because…" on every unadjusted finding would
        bury the ones that did.
        """
        assert build_reason(adjustment(0, [decision_ref(1)])) is None
        assert describe_adjustment(adjustment(0)) is None

    def test_a_capped_finding_that_stayed_in_place_still_has_a_reason(self):
        reason = build_reason(adjustment(0, [decision_ref(1)], capped="rank_move"))
        assert reason is not None
        assert reason.ranks_moved == 0
        assert reason.was_capped
        assert reason.direction == DIRECTION_UP

    def test_zero_displacement_cap_copy_does_not_claim_zero_places(self):
        sentence = render_reason(
            build_reason(adjustment(0, [decision_ref(1)], capped="rank_move"))
        )
        assert "kept this finding in place" in sentence
        assert "zero places" not in sentence

    def test_the_payload_is_json_serialisable(self):
        import json

        payload = describe_adjustment(STORY_CASE)
        assert json.loads(json.dumps(payload)) == payload

    def test_the_payload_can_be_aggregated_without_parsing_prose(self):
        """The point of structuring it: a portfolio view counts, never regexes."""
        payloads = [
            describe_adjustment(STORY_CASE),
            describe_adjustment(adjustment(2, [decision_ref(9, "dismiss")])),
        ]
        moved_up = sum(1 for p in payloads if p["direction"] == "up")
        total_outcomes = sum(p["outcomeCount"] for p in payloads)
        assert moved_up == 1
        assert total_outcomes == 1


# --------------------------------------------------------------------------
# Links (AC2).
# --------------------------------------------------------------------------


class TestLinksToContributingEvidence:
    def test_every_contributing_decision_carries_its_id(self):
        reason = build_reason(STORY_CASE)
        ids = [d.feedback_id for d in reason.contributing_decisions]
        assert ids == [f"fb_{i:03d}" for i in range(4)]

    def test_a_decision_link_resolves_to_the_feedback_entry_route(self):
        """T1 gave every decision a stable id precisely so this can exist."""
        decision = build_reason(STORY_CASE).contributing_decisions[0].to_dict()
        assert decision["href"] == "/api/learning/feedback/entry/fb_000"

    def test_every_contributing_outcome_carries_both_run_ids(self):
        """A2 made both first-class columns so a measured number is auditable."""
        outcome = build_reason(STORY_CASE).contributing_outcomes[0]
        assert outcome.current_run_id == "run_current"
        assert outcome.baseline_run_id == "run_baseline"

    def test_an_outcome_link_resolves_to_the_movement_route(self):
        outcome = build_reason(STORY_CASE).contributing_outcomes[0].to_dict()
        assert outcome["href"] == "/api/opportunity-movement/ident_out"

    def test_an_outcome_carries_its_comparability_verdict(self):
        """A caveated measurement must not present as a clean one."""
        outcome = build_reason(STORY_CASE).contributing_outcomes[0]
        assert outcome.comparability_verdict == "comparable"

    def test_a_reference_without_an_id_has_no_dangling_link(self):
        reason = build_reason(adjustment(-1, [decision_ref(1, feedbackId=None)]))
        assert reason.contributing_decisions[0].to_dict()["href"] is None

    def test_unknown_reference_kinds_are_ignored_rather_than_guessed(self):
        reason = build_reason(
            adjustment(-1, [decision_ref(1), {"kind": "something_new"}, "junk"])
        )
        assert reason.decision_count == 1
        assert reason.outcome_count == 0


# --------------------------------------------------------------------------
# The rendered sentence.
# --------------------------------------------------------------------------


class TestTheRenderedSentence:
    def test_it_reads_as_the_story_writes_it(self):
        sentence = render_reason(build_reason(STORY_CASE))
        assert "Ranked higher" in sentence
        assert "your team accepted four similar findings" in sentence
        assert "one delivered measured improvement" in sentence

    def test_it_states_what_was_done_and_what_was_observed(self):
        sentence = render_reason(build_reason(STORY_CASE))
        assert "moved up two places" in sentence  # what was done
        assert "Based on four decisions and one measured outcome" in sentence

    def test_a_downward_move_reads_as_ranked_lower(self):
        sentence = render_reason(
            build_reason(adjustment(2, [decision_ref(1, "dismiss")]))
        )
        assert sentence.startswith("Ranked lower")

    def test_a_capped_adjustment_says_the_limit_stopped_it(self):
        """The tension case, stated rather than hidden."""
        sentence = render_reason(
            build_reason(adjustment(-3, [decision_ref(1)], capped="rank_move"))
        )
        assert "adjustment limit stopped this" in sentence

    def test_an_unrecognised_verdict_is_reported_not_invented(self):
        """Guessing wording for an unknown verdict is how copy starts lying."""
        sentence = render_reason(
            build_reason(adjustment(-1, [outcome_ref("some_new_verdict")]))
        )
        assert "was measured after action" in sentence

    def test_the_sentence_is_deterministic(self):
        first = render_reason(build_reason(STORY_CASE))
        for _ in range(5):
            assert render_reason(build_reason(STORY_CASE)) == first

    def test_rendering_none_returns_none(self):
        assert render_reason(None) is None

    def test_the_summary_travels_on_the_payload(self):
        """Every surface renders identical wording rather than composing its own."""
        payload = describe_adjustment(STORY_CASE)
        assert payload["summary"] == render_reason(build_reason(STORY_CASE))


# --------------------------------------------------------------------------
# Honesty about thin evidence.
# --------------------------------------------------------------------------


class TestHonestyAboutThinEvidence:
    def test_three_decisions_and_one_outcome_reads_as_limited(self):
        """The case the subtask names by name.

        "Saying so is better than a confident-sounding summary, and it prepares
        the customer for the adjustment changing as more evidence arrives."
        """
        reason = build_reason(
            adjustment(
                -1,
                [decision_ref(i) for i in range(3)] + [outcome_ref()],
                has_outcome=True,
            )
        )
        assert reason.evidence_strength == STRENGTH_LIMITED
        assert reason.is_thin
        sentence = render_reason(reason)
        assert "three decisions and one measured outcome" in sentence
        assert "limited evidence" in sentence
        assert "may change as more arrives" in sentence

    def test_a_single_decision_is_minimal(self):
        reason = build_reason(adjustment(-1, [decision_ref(1)]))
        assert reason.evidence_strength == STRENGTH_MINIMAL
        assert reason.is_thin

    def test_a_well_evidenced_adjustment_does_not_hedge(self):
        reason = build_reason(
            adjustment(
                -2,
                [decision_ref(i) for i in range(6)]
                + [outcome_ref(), outcome_ref("above_band")],
                has_outcome=True,
            )
        )
        assert reason.evidence_strength == STRENGTH_SUBSTANTIAL
        assert not reason.is_thin
        assert "limited evidence" not in render_reason(reason)

    def test_the_counts_are_always_stated_even_when_not_thin(self):
        """The counts are the honesty; the hedge is the extra."""
        sentence = render_reason(build_reason(STORY_CASE))
        assert "Based on" in sentence

    def test_a_measured_outcome_counts_double_for_hedging_only(self):
        """Two decisions hedge; one outcome does not, at the same raw count."""
        two_decisions = build_reason(
            adjustment(-1, [decision_ref(1), decision_ref(2)])
        )
        assert two_decisions.evidence_strength == STRENGTH_LIMITED


# --------------------------------------------------------------------------
# The copy guard.
# --------------------------------------------------------------------------


class TestTheCopyNeverOverclaims:
    @pytest.mark.parametrize(
        "text,category",
        [
            ("We learned this is more important for you.", CATEGORY_KNOWLEDGE_CLAIM),
            ("AgentIQ understands your priorities.", CATEGORY_KNOWLEDGE_CLAIM),
            ("Personalised for your organisation.", CATEGORY_KNOWLEDGE_CLAIM),
            ("This is more valuable to your team.", CATEGORY_IMPORTANCE_CLAIM),
            ("This should be prioritised.", CATEGORY_IMPORTANCE_CLAIM),
            ("We recommend tackling this first.", CATEGORY_IMPORTANCE_CLAIM),
            (
                "Your decisions corroborate this finding.",
                CATEGORY_CREDIBILITY_IMPLICATION,
            ),
            (
                "We are more confident in this finding.",
                CATEGORY_CREDIBILITY_IMPLICATION,
            ),
            ("Your team's decisions confirm this.", CATEGORY_CREDIBILITY_IMPLICATION),
            (
                "This finding is proven by your decisions.",
                CATEGORY_CREDIBILITY_IMPLICATION,
            ),
            ("Your outcomes validate that.", CATEGORY_CREDIBILITY_IMPLICATION),
            ("Stronger evidence supports this.", CATEGORY_CREDIBILITY_IMPLICATION),
        ],
    )
    def test_overclaiming_copy_is_flagged(self, text, category):
        violations = scan_text(text)
        assert violations, f"not flagged: {text!r}"
        assert any(v.category == category for v in violations)

    @pytest.mark.parametrize(
        "text",
        [
            "Ranked higher: your team accepted 4 similar findings and one "
            "delivered measured improvement.",
            "Moved up two places, based on four decisions and one measured outcome.",
            "Your team dismissed three similar findings.",
            "The adjustment limit stopped this moving further than three places.",
            "One similar finding did not move as far as projected.",
            "Based on three decisions, which is limited evidence.",
            # The SHIPPED roadmap stage summaries. Copy this guard does not own,
            # and a false positive here is the exact failure A1 T5 warns about —
            # "Prove value fast" tripped a bare-verb `verification_claim` rule
            # before it was narrowed to require an object.
            "Prove value fast with low-effort quick wins.",
            "Scale into strategic pilots with cross-team alignment.",
            "Invest in complex opportunities requiring deeper data + governance.",
            "Recommend reviewing the adjustment history before reset.",
        ],
    )
    def test_legitimate_copy_is_not_flagged(self, text):
        """A guard that flags the evidence trains people to ignore it (A1 T5)."""
        assert scan_text(text) == [], f"false positive on {text!r}"

    def test_a_findings_own_corroboration_label_is_not_learning_copy(self):
        """The scope lesson CI taught, pinned.

        A finding legitimately carrying "Corroborated across ServiceNow and Jira"
        matches ``corroboration_implication`` in isolation — and it SHOULD, because
        learning copy claiming corroboration is exactly what that rule is for. The
        answer is not to narrow the rule (that would blind it to the real failure)
        but to scope the SWEEP to what learning wrote. This test states the rule's
        behaviour so nobody "fixes" it the wrong way.
        """
        assert scan_text("Corroborated across ServiceNow and Jira")

    def test_the_shipped_roadmap_stage_copy_scans_clean(self):
        """Copy owned by another feature must not trip this guard.

        Scoped to the STAGE prose the roadmap engine authors — the findings it
        carries belong to other features and are swept by placement checks
        instead. This is the narrower, correct version of a whole-payload sweep
        that CI showed was over-reaching.
        """
        from app.learning_reason_vocabulary import scan_payload
        from app.roadmap_engine import build_roadmap

        opps = [
            {
                "id": f"opp_{i}",
                "opportunity_identity": f"ident_{i}",
                "title": f"Finding {i}",
                "tier": tier,
                "impact": 7,
                "effort": 3,
                "confidence": "HIGH",
                "aiRationale": "Case ownership changes cluster on one queue.",
                "evidenceIds": [],
                "decision": decision,
                "packId": "service_cloud",
                "requiredPermissions": ["Salesforce: read Case"],
                "override": {
                    "isLocked": False,
                    "rationaleOverride": "",
                    "overrideReason": "",
                    "updatedAt": None,
                },
                "_debug": {"detector_id": "D"},
            }
            for i, (tier, decision) in enumerate(
                [
                    ("Quick Win", "APPROVED"),
                    ("Strategic", "UNREVIEWED"),
                    ("Complex", "UNREVIEWED"),
                ]
            )
        ]
        roadmap = build_roadmap(opps)
        # Only the stage prose the roadmap engine itself writes.
        stage_copy = [
            {"title": stage.get("title"), "summary": stage.get("summary")}
            for stage in roadmap["stages"]
        ]
        violations = scan_payload(stage_copy)
        assert violations == [], [str(v) for v in violations]

    def test_every_rendered_sentence_passes_its_own_guard(self):
        """Our templates are clean by construction, not scrubbed at the edge."""
        cases = [
            STORY_CASE,
            adjustment(3, [decision_ref(1, "dismiss")], capped="score_fraction"),
            adjustment(-1, [outcome_ref("below_band")], has_outcome=True),
            adjustment(1, [decision_ref(1, "defer", reasonCode="lower_priority")]),
            adjustment(-2, [outcome_ref("not_projected")], has_outcome=True),
        ]
        for case in cases:
            assert_clean(render_reason(build_reason(case)), where="rendered reason")

    def test_the_guard_runs_at_build_time_not_only_in_tests(self):
        """render_reason checks its own output before returning it."""
        import inspect

        from app import learning_reason

        source = inspect.getsource(learning_reason.render_reason)
        assert "assert_clean" in source

    def test_a_payload_sweep_finds_nested_violations(self):
        payload = {"reason": {"summary": "We learned this matters more to you."}}
        violations = scan_payload(payload)
        assert violations
        assert violations[0].path.startswith("reason.summary")

    def test_machine_identifiers_are_not_swept_as_prose(self):
        """A verdict code is not customer-facing copy."""
        payload = {"verdict": "within_band", "action": "accept", "cappedBy": "rank_move"}
        assert scan_payload(payload) == []

    def test_assert_clean_raises_on_our_own_bad_copy(self):
        with pytest.raises(ProhibitedLearningCopyError):
            assert_clean("We learned this is more important.", where="test")

    def test_contains_prohibited_is_a_convenience_over_scan(self):
        assert contains_prohibited("We recommend this.")
        assert not contains_prohibited("Ranked higher: moved up two places.")


# --------------------------------------------------------------------------
# The boundary — the reason explains ordering and nothing else.
# --------------------------------------------------------------------------


class TestTheReasonStaysOutOfTheCredibilityFields:
    def test_the_credibility_field_list_covers_the_ac3_trio(self):
        for field in ("confidence", "corroboration_sources", "evidenceIds"):
            assert field in CREDIBILITY_FIELDS

    def test_a_clean_finding_reports_no_placement_violation(self):
        finding = {
            "confidence": "HIGH",
            "corroboration_label": "Corroborated across ServiceNow and Jira",
            "evidenceIds": ["ev_1"],
            "_ranking": {"reason": {"summary": "Ranked higher: moved up two places."}},
        }
        assert reason_placement_violations(finding) == []

    def test_a_reason_nested_under_confidence_is_a_violation(self):
        finding = {"confidence": {"level": "HIGH", "reason": {"summary": "x"}}}
        assert "confidence" in reason_placement_violations(finding)

    def test_ranking_copy_written_into_a_narrative_field_is_a_violation(self):
        """The AC3-spirit failure: copy implying learning aided credibility."""
        finding = {
            "aiRationale": "Ranked higher because your team accepted 4 similar findings."
        }
        assert "aiRationale" in reason_placement_violations(finding)

    def test_the_reason_is_namespaced_under_ranking_in_the_annotation(self):
        from app.learning_adjustment import RANKING_FIELD, GroupAdjustment, adjust_ranking

        items = [
            {"id": f"o{i}", "impact": 7, "packId": "p", "_debug": {"detector_id": "d"}}
            for i in range(6)
        ]
        items[4]["_debug"] = {"detector_id": "fav"}
        state = {
            ("fav", "p"): GroupAdjustment(
                "fav", "p", net_weight=6.0, contributing_refs=(decision_ref(1),)
            )
        }
        result = adjust_ranking(items, state)
        moved = [o for o in result.ordered if o[RANKING_FIELD].get("moved")]
        assert moved, "no finding moved, so placement is untested"
        for served in moved:
            assert "reason" in served[RANKING_FIELD]
            assert "reason" not in served
            assert reason_placement_violations(served) == []
