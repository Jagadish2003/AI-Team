"""2.0-A3 T1 — the learning signal set's weighting behaviour.

Pure unit tests: ``collect_learning_signals`` takes its record sequences as
arguments and ``recency_multiplier`` takes ``now``, precisely so the weighting
logic is testable without a database or a frozen clock.

What is being protected here is the sentence that makes this feature defensible:
**an outcome outweighs an opinion**. Everything else — decay, caveat handling,
similarity, cold start — exists to keep that sentence true under real data, where
measurements are caveated and decisions are stale.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.learning_signal_config import (
    DIRECTION_NEGATIVE,
    DIRECTION_NEUTRAL,
    DIRECTION_POSITIVE,
    LearningConfigError,
    load_config,
    parse_config,
    validate_config,
)
from app.learning_signals import (
    EXCLUDED_NEUTRAL_VERDICT,
    EXCLUDED_UNWEIGHTED_DEFER_REASON,
    SOURCE_DECISION,
    SOURCE_OUTCOME,
    SimilarityKey,
    are_similar,
    collect_learning_signals,
    decision_signal,
    describe_signal_set,
    group_by_similarity,
    outcome_signal,
    recency_multiplier,
    similarity_key,
    similarity_score,
)

NOW = datetime(2026, 8, 3, 12, 0, 0, tzinfo=timezone.utc)
CONFIG = load_config()


def days_ago(n: int) -> str:
    return (NOW - timedelta(days=n)).isoformat()


def a_decision(**overrides):
    record = {
        "feedbackId": "fb_001",
        "opportunityIdentity": "opp_abc",
        "action": "accept",
        "reasonCode": None,
        "actorId": "analyst_1",
        "detectorId": "HANDOFF_FRICTION",
        "packId": "service_cloud",
        "recordedAt": days_ago(1),
    }
    record.update(overrides)
    return record


def an_outcome(**overrides):
    record = {
        "opportunityIdentity": "opp_abc",
        "detectorId": "HANDOFF_FRICTION",
        "currentRunId": "run_9",
        "baselineRunId": "run_1",
        "measuredAt": days_ago(1),
        # Real shape: the pack lives under `projected` (see
        # projection_validation._projection_block), not at the top level.
        "projectionValidation": {
            "verdict": "within_band",
            "projected": {"packId": "service_cloud", "packVersion": "1.2.0"},
        },
        "comparability": {"verdict": "comparable"},
        "confounderSummary": {"count": 0, "materialCount": 0, "advisoryCount": 0},
    }
    record.update(overrides)
    return record


# --------------------------------------------------------------------------
# The governing principle.
# --------------------------------------------------------------------------


class TestAnOutcomeOutweighsAnOpinion:
    def test_the_weakest_outcome_beats_the_strongest_decision(self):
        """The relationship the whole feature rests on, in the shipped config."""
        outcomes = [s.weight for s in CONFIG.outcome_signals.values() if s.weight > 0]
        decisions = [s.weight for s in CONFIG.decision_signals.values()]
        assert min(outcomes) > max(decisions), (
            "a decision weight has reached an outcome weight; outcome-weighted "
            "learning is what separates this from click-tracking"
        )

    def test_a_measured_outcome_outweighs_an_accept_at_equal_age(self):
        """End to end, not just in the config numbers."""
        outcome = outcome_signal(an_outcome(), now=NOW, config=CONFIG)
        decision = decision_signal(a_decision(), now=NOW, config=CONFIG)
        assert outcome.weight > decision.weight

    def test_a_caveated_outcome_still_outweighs_a_fresh_accept(self):
        """The interesting case: caveats must not demote an outcome below opinion.

        A weakly-comparable measurement with both severities of caveat is the
        worst realistic outcome signal. If down-weighting could push it below a
        clean analyst accept, the ordering guarantee would hold only in the
        config and not in the data.
        """
        worst = outcome_signal(
            an_outcome(
                projectionValidation={"verdict": "below_band"},
                comparability={"verdict": "weakly_comparable"},
                confounderSummary={"count": 3, "materialCount": 1, "advisoryCount": 2},
            ),
            now=NOW,
            config=CONFIG,
        )
        fresh_accept = decision_signal(
            a_decision(recordedAt=NOW.isoformat()), now=NOW, config=CONFIG
        )
        assert worst.weight > 0
        assert worst.weight > fresh_accept.weight

    def test_at_equal_age_every_outcome_outweighs_every_decision(self):
        """The invariant, exhaustively — not just the one case that broke it.

        Cross product of every verdict, comparability verdict and caveat mix
        against every action and defer reason, all at the same instant. The
        anecdotal version of this test passed while a below-band,
        weakly-comparable, doubly-caveated measurement sat below a clean accept.
        """
        outcomes = [
            outcome_signal(
                an_outcome(
                    projectionValidation={"verdict": verdict},
                    comparability={"verdict": comparability},
                    confounderSummary={
                        "count": material + advisory,
                        "materialCount": material,
                        "advisoryCount": advisory,
                    },
                    measuredAt=NOW.isoformat(),
                ),
                now=NOW,
                config=CONFIG,
            )
            for verdict in ("within_band", "above_band", "below_band")
            for comparability in ("comparable", "weakly_comparable", "not_comparable")
            for material in (0, 1, 5)
            for advisory in (0, 1, 5)
        ]
        decisions = [
            decision_signal(
                a_decision(action=action, reasonCode=reason, recordedAt=NOW.isoformat()),
                now=NOW,
                config=CONFIG,
            )
            for action, reason in [
                ("accept", None),
                ("dismiss", None),
                *[("defer", r) for r in CONFIG.defer_reasons],
            ]
        ]

        weakest_outcome = min(s.weight for s in outcomes)
        strongest_decision = max(s.weight for s in decisions)
        assert weakest_outcome > strongest_decision, (
            f"the weakest of {len(outcomes)} outcome combinations ({weakest_outcome}) "
            f"does not outweigh the strongest of {len(decisions)} decisions "
            f"({strongest_decision}) — the governing principle is broken in the "
            "data even though the config validates"
        )

    def test_the_floor_is_applied_before_decay_so_stale_signals_fade_together(self):
        """Both classes decay identically, so their order survives age.

        Flooring AFTER decay would instead claim a three-year-old measurement
        about a since-rebuilt system beats a fresh judgement about today's.
        """
        stale_outcome = outcome_signal(
            an_outcome(
                projectionValidation={"verdict": "below_band"},
                comparability={"verdict": "not_comparable"},
                measuredAt=days_ago(1000),
            ),
            now=NOW,
            config=CONFIG,
        )
        stale_accept = decision_signal(
            a_decision(recordedAt=days_ago(1000)), now=NOW, config=CONFIG
        )
        fresh_accept = decision_signal(
            a_decision(recordedAt=NOW.isoformat()), now=NOW, config=CONFIG
        )
        assert stale_outcome.weight > stale_accept.weight
        assert stale_outcome.weight < fresh_accept.weight, (
            "an ancient measurement must not outrank a fresh judgement about "
            "today's system — decay is what expresses that, and the floor must "
            "not defeat it"
        )

    def test_the_floor_is_reported_when_it_binds(self):
        """A weight nobody can explain is not usable in an explainability feature."""
        floored = outcome_signal(
            an_outcome(
                projectionValidation={"verdict": "below_band"},
                comparability={"verdict": "not_comparable"},
                confounderSummary={"count": 2, "materialCount": 1, "advisoryCount": 1},
            ),
            now=NOW,
            config=CONFIG,
        )
        clean = outcome_signal(an_outcome(), now=NOW, config=CONFIG)
        assert "outcomeFloor" in floored.multipliers
        assert "outcomeFloor" not in clean.multipliers, (
            "the floor must not be reported when it did not bind — a multiplier "
            "list that always mentions it teaches a reader nothing"
        )

    def test_a_config_whose_floor_ratio_does_not_exceed_one_is_refused(self):
        raw = {
            "outcome_signals": {"below_band": {"weight": 2.0, "direction": "negative"}},
            "decision_signals": {"accept": {"weight": 1.0, "direction": "positive"}},
            "outcome_floor": {"ratio_to_strongest_decision": 1.0},
        }
        with pytest.raises(LearningConfigError) as excinfo:
            validate_config(parse_config(raw))
        assert "must exceed 1.0" in str(excinfo.value)

    def test_a_config_that_inverts_the_principle_is_refused(self):
        """Not documented — refused, at load.

        This is the edit that would change what the feature is while nothing in
        the product looked different.
        """
        raw = {
            "outcome_signals": {"within_band": {"weight": 1.0, "direction": "positive"}},
            "decision_signals": {"accept": {"weight": 1.0, "direction": "positive"}},
        }
        with pytest.raises(LearningConfigError) as excinfo:
            validate_config(parse_config(raw))
        assert "outcome must outweigh an opinion" in str(excinfo.value)

    def test_a_config_with_a_zero_comparability_multiplier_is_refused(self):
        """A zero multiplier silently discards a caveated measurement.

        That is the blocking 2.0-A2 T3 explicitly refused, re-introduced one
        layer up where nobody would look for it.
        """
        raw = {
            "outcome_signals": {"within_band": {"weight": 3.0, "direction": "positive"}},
            "decision_signals": {"accept": {"weight": 1.0, "direction": "positive"}},
            "comparability": {"comparable": 1.0, "not_comparable": 0.0},
        }
        with pytest.raises(LearningConfigError) as excinfo:
            validate_config(parse_config(raw))
        assert "silently discards" in str(excinfo.value)

    def test_the_shipped_config_loads_and_validates(self):
        validate_config(CONFIG)

    def test_every_config_section_declares_its_basis(self):
        """A reader must be able to tell a calibrated number from a first guess."""
        for section in (
            "outcome_signals",
            "decision_signals",
            "defer_reasons",
            "recency",
            "comparability",
            "cold_start",
            "similarity",
        ):
            assert CONFIG.basis_for(section), f"{section} declares no basis"

    def test_the_weights_are_honestly_marked_provisional(self):
        """No production outcome data exists yet; the config must say so."""
        assert CONFIG.is_provisional("outcome_signals")
        assert CONFIG.is_provisional("decision_signals")


# --------------------------------------------------------------------------
# Outcome signals.
# --------------------------------------------------------------------------


class TestOutcomeSignals:
    @pytest.mark.parametrize(
        "verdict,direction",
        [
            ("within_band", DIRECTION_POSITIVE),
            ("above_band", DIRECTION_POSITIVE),
            ("below_band", DIRECTION_NEGATIVE),
        ],
    )
    def test_verdicts_carry_the_expected_direction(self, verdict, direction):
        signal = outcome_signal(
            an_outcome(projectionValidation={"verdict": verdict}),
            now=NOW,
            config=CONFIG,
        )
        assert signal.direction == direction

    def test_within_band_outranks_above_band(self):
        """A verified projection is stronger evidence than an overshoot.

        Rewarding a miss as highly as a hit would teach the ranking layer
        nothing about calibration — and A1's band width is itself calibrated
        from these outcomes.
        """
        within = outcome_signal(an_outcome(), now=NOW, config=CONFIG)
        above = outcome_signal(
            an_outcome(projectionValidation={"verdict": "above_band"}),
            now=NOW,
            config=CONFIG,
        )
        assert within.weight > above.weight

    def test_too_early_is_counted_but_carries_no_weight(self):
        """Counted, not dropped: a signal that vanishes is one nobody can ask about.

        ``too_early`` really does teach nothing — the horizon has not elapsed, so
        learning from it means learning from an unfinished experiment.
        """
        signal = outcome_signal(
            an_outcome(projectionValidation={"verdict": "too_early"}),
            now=NOW,
            config=CONFIG,
        )
        assert signal is not None
        assert signal.weight == 0.0
        assert signal.direction == DIRECTION_NEUTRAL
        assert signal.excluded_reason == EXCLUDED_NEUTRAL_VERDICT


class TestMeasuredDirectionWhenNothingWasProjected:
    """A measurement with no projection is still a measurement.

    The verdict answers "was our model right?"; the direction answers "did the
    action help?" — and ranking cares about the second. Zeroing these would make
    every finding created before 2.0-A1 shipped permanently unlearnable.
    """

    @staticmethod
    def unprojected(direction, role="movement"):
        return an_outcome(
            projectionValidation={"verdict": "not_projected"},
            movements=[
                {"signalName": "owner_changes_90d", "role": role, "direction": direction}
            ],
        )

    @pytest.mark.parametrize(
        "direction,expected",
        [
            ("improved", DIRECTION_POSITIVE),
            ("worsened", DIRECTION_NEGATIVE),
            ("unchanged", DIRECTION_NEGATIVE),
        ],
    )
    def test_the_measured_direction_carries_the_signal(self, direction, expected):
        signal = outcome_signal(self.unprojected(direction), now=NOW, config=CONFIG)
        assert signal.weight > 0
        assert signal.direction == expected
        assert signal.excluded_reason is None

    def test_a_directional_outcome_still_outweighs_every_decision(self):
        """It is an outcome, so the class boundary applies to it too."""
        weakest = min(
            outcome_signal(self.unprojected(d), now=NOW, config=CONFIG).weight
            for d in ("improved", "worsened", "unchanged")
        )
        strongest_decision = max(
            decision_signal(
                a_decision(action=action, reasonCode=reason, recordedAt=days_ago(1)),
                now=NOW,
                config=CONFIG,
            ).weight
            for action, reason in [
                ("accept", None),
                ("dismiss", None),
                *[("defer", r) for r in CONFIG.defer_reasons],
            ]
        )
        assert weakest > strongest_decision

    def test_a_directional_outcome_weighs_less_than_a_validated_one(self):
        """A direction with no expectation to compare against is weaker evidence."""
        validated = outcome_signal(an_outcome(), now=NOW, config=CONFIG)
        directional = outcome_signal(
            self.unprojected("improved"), now=NOW, config=CONFIG
        )
        assert directional.weight < validated.weight

    def test_the_direction_is_not_used_when_a_projection_exists(self):
        """The band verdict already incorporates it and knows what was expected.

        Applying both would count one measurement twice.
        """
        with_projection = an_outcome(
            movements=[
                {"signalName": "x", "role": "movement", "direction": "worsened"}
            ]
        )
        signal = outcome_signal(with_projection, now=NOW, config=CONFIG)
        assert signal.direction == DIRECTION_POSITIVE, (
            "the within_band verdict must win over a raw 'worsened' direction"
        )
        assert signal.evidence_ref["measuredDirection"] is None

    def test_the_movement_role_signal_is_preferred_over_a_population_one(self):
        """A denominator moving says nothing about whether the intervention worked."""
        record = an_outcome(
            projectionValidation={"verdict": "not_projected"},
            movements=[
                {"signalName": "total_cases", "role": "population", "direction": "worsened"},
                {"signalName": "owner_changes", "role": "movement", "direction": "improved"},
            ],
        )
        signal = outcome_signal(record, now=NOW, config=CONFIG)
        assert signal.direction == DIRECTION_POSITIVE
        assert signal.evidence_ref["measuredDirection"] == "improved"

    def test_an_unroled_signal_is_used_when_no_movement_role_is_marked(self):
        signal = outcome_signal(
            self.unprojected("improved", role=None), now=NOW, config=CONFIG
        )
        assert signal.weight > 0

    def test_an_unknown_direction_carries_nothing(self):
        signal = outcome_signal(self.unprojected("unknown"), now=NOW, config=CONFIG)
        assert signal.weight == 0.0
        assert signal.excluded_reason == EXCLUDED_NEUTRAL_VERDICT

    def test_a_measurement_with_no_movements_at_all_carries_nothing(self):
        signal = outcome_signal(
            an_outcome(projectionValidation={"verdict": "not_projected"}, movements=[]),
            now=NOW,
            config=CONFIG,
        )
        assert signal.weight == 0.0

    def test_the_label_describes_the_direction_not_the_missing_projection(self):
        """Destined for a customer-facing 'why'."""
        signal = outcome_signal(
            self.unprojected("improved"), now=NOW, config=CONFIG
        )
        assert "improved measurably" in signal.label


class TestOutcomeSignalsContinued:

    def test_a_weakly_comparable_measurement_is_down_weighted_not_dropped(self):
        clean = outcome_signal(an_outcome(), now=NOW, config=CONFIG)
        weak = outcome_signal(
            an_outcome(comparability={"verdict": "weakly_comparable"}),
            now=NOW,
            config=CONFIG,
        )
        assert 0 < weak.weight < clean.weight
        assert weak.multipliers["comparability"] < 1.0

    def test_a_not_comparable_measurement_still_carries_weight(self):
        """A2 T3's rule: a poor verdict still REPORTS, with the caveat attached."""
        signal = outcome_signal(
            an_outcome(comparability={"verdict": "not_comparable"}),
            now=NOW,
            config=CONFIG,
        )
        assert signal.weight > 0

    def test_an_unknown_comparability_verdict_takes_the_most_conservative_multiplier(
        self,
    ):
        """A verdict this code does not recognise must not read as a clean one."""
        unknown = outcome_signal(
            an_outcome(comparability={"verdict": "something_new"}),
            now=NOW,
            config=CONFIG,
        )
        assert unknown.multipliers["comparability"] == min(CONFIG.comparability.values())

    def test_confounders_down_weight_once_per_severity_not_once_per_caveat(self):
        """Six advisory caveats must not weight a measurement into oblivion."""
        one = outcome_signal(
            an_outcome(
                confounderSummary={"count": 1, "materialCount": 0, "advisoryCount": 1}
            ),
            now=NOW,
            config=CONFIG,
        )
        six = outcome_signal(
            an_outcome(
                confounderSummary={"count": 6, "materialCount": 0, "advisoryCount": 6}
            ),
            now=NOW,
            config=CONFIG,
        )
        assert one.multipliers["confounders"] == six.multipliers["confounders"]

    def test_a_material_caveat_weighs_less_than_an_advisory_one(self):
        material = outcome_signal(
            an_outcome(
                confounderSummary={"count": 1, "materialCount": 1, "advisoryCount": 0}
            ),
            now=NOW,
            config=CONFIG,
        )
        advisory = outcome_signal(
            an_outcome(
                confounderSummary={"count": 1, "materialCount": 0, "advisoryCount": 1}
            ),
            now=NOW,
            config=CONFIG,
        )
        assert material.weight < advisory.weight

    def test_an_outcome_without_an_identity_produces_no_signal(self):
        assert outcome_signal(an_outcome(opportunityIdentity=""), config=CONFIG) is None

    def test_the_pack_is_read_from_where_a2_actually_stores_it(self):
        """A real validation record nests the pack under ``projected``.

        Reading the wrong level fails silently: every outcome would carry
        ``pack_id=None``, bucketing outcomes apart from the decisions about the
        same findings and dropping their similarity from 1.0 to the weaker
        "same detector, other pack" tier. Nothing would raise, and the ranking
        would simply be built on a join that never matched.
        """
        signal = outcome_signal(an_outcome(), now=NOW, config=CONFIG)
        assert signal.pack_id == "service_cloud"

    def test_an_outcome_and_a_decision_about_one_finding_are_maximally_similar(self):
        """The join the whole layer depends on, at the similarity level."""
        outcome = outcome_signal(an_outcome(), now=NOW, config=CONFIG)
        decision = decision_signal(a_decision(), now=NOW, config=CONFIG)
        assert (
            similarity_score(
                similarity_key(outcome.detector_id, outcome.pack_id),
                similarity_key(decision.detector_id, decision.pack_id),
                CONFIG,
            )
            == 1.0
        )

    def test_the_evidence_ref_names_both_runs(self):
        """AC2: the link back to the contributing outcome."""
        ref = outcome_signal(an_outcome(), now=NOW, config=CONFIG).evidence_ref
        assert ref["kind"] == SOURCE_OUTCOME
        assert ref["currentRunId"] == "run_9"
        assert ref["baselineRunId"] == "run_1"
        assert ref["verdict"] == "within_band"


# --------------------------------------------------------------------------
# Decision signals.
# --------------------------------------------------------------------------


class TestDecisionSignals:
    def test_accept_is_positive_and_dismiss_is_negative(self):
        accept = decision_signal(a_decision(), now=NOW, config=CONFIG)
        dismiss = decision_signal(
            a_decision(action="dismiss"), now=NOW, config=CONFIG
        )
        assert accept.direction == DIRECTION_POSITIVE
        assert dismiss.direction == DIRECTION_NEGATIVE

    def test_accept_and_dismiss_are_symmetric(self):
        """Asymmetry would bias the layer towards surfacing more.

        A team that dismisses a whole finding type is telling the platform
        something exactly as informative as a team that accepts one.
        """
        accept = decision_signal(a_decision(), now=NOW, config=CONFIG)
        dismiss = decision_signal(a_decision(action="dismiss"), now=NOW, config=CONFIG)
        assert accept.weight == dismiss.weight
        assert accept.signed_weight == -dismiss.signed_weight

    def test_defer_weighs_less_than_dismiss(self):
        """'Not now' is genuinely weaker than 'no' — but it is not neutral."""
        dismiss = decision_signal(a_decision(action="dismiss"), now=NOW, config=CONFIG)
        defer = decision_signal(
            a_decision(action="defer", reasonCode="lower_priority"),
            now=NOW,
            config=CONFIG,
        )
        assert 0 < defer.weight < dismiss.weight

    @pytest.mark.parametrize(
        "reason", ["no_capacity", "blocked_by_dependency", "awaiting_approval"]
    )
    def test_calendar_reasons_carry_no_learning_weight(self, reason):
        """A resourcing fact about the team is not a judgement about the finding.

        Recorded and visible; never learned from. Learning from these would
        demote findings for reasons entirely about the customer's calendar.
        """
        signal = decision_signal(
            a_decision(action="defer", reasonCode=reason), now=NOW, config=CONFIG
        )
        assert signal.weight == 0.0
        assert signal.excluded_reason == EXCLUDED_UNWEIGHTED_DEFER_REASON
        assert signal.evidence_ref["reasonCode"] == reason, "still linkable"

    def test_a_value_judgement_defer_does_carry_weight(self):
        for reason in ("lower_priority", "needs_more_evidence", "timing_not_right"):
            signal = decision_signal(
                a_decision(action="defer", reasonCode=reason), now=NOW, config=CONFIG
            )
            assert signal.weight > 0, reason

    def test_lower_priority_is_the_strongest_defer_reason(self):
        """An explicit relative-value judgement is nearly a dismissal."""
        weights = {
            reason: decision_signal(
                a_decision(action="defer", reasonCode=reason), now=NOW, config=CONFIG
            ).weight
            for reason in ("lower_priority", "needs_more_evidence", "timing_not_right")
        }
        assert weights["lower_priority"] == max(weights.values())

    def test_a_defer_with_an_unrecognised_reason_carries_nothing(self):
        """Guessing a weight for an unknown is how a layer starts learning noise."""
        signal = decision_signal(
            a_decision(action="defer", reasonCode="because_i_said_so"),
            now=NOW,
            config=CONFIG,
        )
        assert signal.weight == 0.0

    def test_the_evidence_ref_names_the_decision_and_its_actor(self):
        """AC2: the link back to the contributing decision."""
        ref = decision_signal(a_decision(), now=NOW, config=CONFIG).evidence_ref
        assert ref["kind"] == SOURCE_DECISION
        assert ref["feedbackId"] == "fb_001"
        assert ref["actorId"] == "analyst_1"

    def test_every_signal_carries_a_plain_language_label(self):
        """Destined for a customer-facing 'why', not a log line."""
        for record in (a_decision(), a_decision(action="dismiss")):
            signal = decision_signal(record, now=NOW, config=CONFIG)
            assert "your team" in signal.label


# --------------------------------------------------------------------------
# Recency.
# --------------------------------------------------------------------------


class TestRecency:
    def test_a_fresh_signal_is_undecayed(self):
        assert recency_multiplier(NOW.isoformat(), now=NOW, config=CONFIG) == 1.0

    def test_one_half_life_halves_the_weight(self):
        half_life = CONFIG.recency.half_life_days
        value = recency_multiplier(days_ago(int(half_life)), now=NOW, config=CONFIG)
        assert value == pytest.approx(0.5, abs=0.01)

    def test_decay_never_reaches_zero(self):
        """A signal that decayed to zero would silently leave the explanation.

        The customer would see a reason cite four decisions, then three, with
        nothing having changed and no event to point at.
        """
        ancient = recency_multiplier(days_ago(4000), now=NOW, config=CONFIG)
        assert ancient == CONFIG.recency.floor
        assert ancient > 0

    def test_an_undated_signal_is_treated_as_fully_decayed(self):
        """The conservative direction — the alternative rewards missing data."""
        assert recency_multiplier(None, now=NOW, config=CONFIG) == CONFIG.recency.floor

    def test_an_unparseable_timestamp_does_not_raise(self):
        assert recency_multiplier("not a date", now=NOW, config=CONFIG) > 0

    def test_a_stale_accept_weighs_less_than_a_fresh_one(self):
        fresh = decision_signal(
            a_decision(recordedAt=NOW.isoformat()), now=NOW, config=CONFIG
        )
        stale = decision_signal(
            a_decision(recordedAt=days_ago(720)), now=NOW, config=CONFIG
        )
        assert stale.weight < fresh.weight
        assert stale.weight > 0


# --------------------------------------------------------------------------
# Similarity.
# --------------------------------------------------------------------------


class TestSimilarity:
    def test_same_detector_same_pack_is_the_strongest_match(self):
        key = similarity_key("HANDOFF_FRICTION", "service_cloud")
        assert similarity_score(key, key, CONFIG) == 1.0

    def test_the_same_detector_in_another_pack_is_weaker(self):
        left = similarity_key("HANDOFF_FRICTION", "service_cloud")
        right = similarity_key("HANDOFF_FRICTION", "financial_services_cloud")
        score = similarity_score(left, right, CONFIG)
        assert 0 < score < 1.0
        assert are_similar(left, right, CONFIG)

    def test_two_findings_sharing_only_a_pack_are_not_similar(self):
        """There is deliberately no 'same pack' tier.

        Two findings that share only a pack have nothing meaningful in common,
        and calling them similar in a customer-facing explanation would be
        indefensible.
        """
        left = similarity_key("HANDOFF_FRICTION", "service_cloud")
        right = SimilarityKey(
            detector_id="approval_bottleneck",
            pack_id="service_cloud",
            signal_concept="something_else",
        )
        assert similarity_score(left, right, CONFIG) == 0.0
        assert not are_similar(left, right, CONFIG)

    def test_a_shared_signal_concept_is_the_weakest_admissible_match(self):
        left = SimilarityKey("detector_a", "pack_a", "reassignment_count")
        right = SimilarityKey("detector_b", "pack_b", "reassignment_count")
        score = similarity_score(left, right, CONFIG)
        assert score == CONFIG.similarity.same_signal_concept
        assert score >= CONFIG.similarity.minimum_score

    def test_similarity_never_reads_a_title_or_narrative(self):
        """A name-similarity match is the silent fuzzy inference 2.0-B2 refuses.

        The key is built only from run-invariant identifiers, so two findings
        with identical titles and different detectors are not similar.
        """
        left = SimilarityKey("detector_a", "pack_a", None)
        right = SimilarityKey("detector_b", "pack_a", None)
        assert similarity_score(left, right, CONFIG) == 0.0

    def test_the_signal_concept_comes_from_the_a1_registry(self):
        """Reusing A1's registry rather than inventing a second mapping."""
        from discovery.projection.signal_registry import get_detector_profile

        profile = get_detector_profile("HANDOFF_FRICTION")
        key = similarity_key("HANDOFF_FRICTION", "service_cloud")
        assert key.signal_concept == profile.movement_signal.lower()

    def test_an_unprofiled_detector_has_no_concept_and_is_similar_only_to_itself(self):
        key = similarity_key("NO_SUCH_DETECTOR_XYZ", "pack_a")
        assert key.signal_concept is None
        other = similarity_key("ANOTHER_UNPROFILED_ABC", "pack_a")
        assert similarity_score(key, other, CONFIG) == 0.0


# --------------------------------------------------------------------------
# The set: cold start, grouping, isolation.
# --------------------------------------------------------------------------


def a_set(decisions=(), outcomes=()):
    return collect_learning_signals(
        "org_1",
        now=NOW,
        config=CONFIG,
        decision_records=list(decisions),
        outcome_records=list(outcomes),
    )


class TestColdStart:
    def test_a_handful_of_decisions_does_not_activate_learning(self):
        """AC4: no pretending to personalise from three data points."""
        signal_set = a_set(
            decisions=[
                a_decision(feedbackId=f"fb_{i}", opportunityIdentity=f"opp_{i}")
                for i in range(3)
            ]
        )
        assert not signal_set.is_active
        assert "not yet active" in signal_set.inactive_reason

    def test_one_opportunity_reviewed_many_times_does_not_activate_learning(self):
        """The rule that stops a single finding switching learning on for an org.

        ``latest_feedback_by_identity`` already collapses repeat decisions on one
        opportunity, but the distinct-identity threshold is the structural
        backstop if a caller ever passes raw history instead.
        """
        signal_set = a_set(
            decisions=[
                a_decision(feedbackId=f"fb_{i}", opportunityIdentity="opp_same")
                for i in range(40)
            ]
        )
        assert signal_set.distinct_identities == 1
        assert not signal_set.is_active
        assert "distinct findings" in signal_set.inactive_reason

    def test_enough_signals_across_enough_findings_activates_learning(self):
        signal_set = a_set(
            decisions=[
                a_decision(feedbackId=f"fb_{i}", opportunityIdentity=f"opp_{i}")
                for i in range(CONFIG.cold_start.minimum_signals)
            ]
        )
        assert signal_set.is_active
        assert signal_set.inactive_reason is None

    def test_unweighted_signals_do_not_count_towards_the_threshold(self):
        """A zero-weight signal informs nothing and must not unlock learning."""
        signal_set = a_set(
            decisions=[
                a_decision(
                    feedbackId=f"fb_{i}",
                    opportunityIdentity=f"opp_{i}",
                    action="defer",
                    reasonCode="no_capacity",
                )
                for i in range(40)
            ]
        )
        assert len(signal_set.signals) == 40
        assert len(signal_set.weighted) == 0
        assert not signal_set.is_active

    def test_an_empty_org_reports_inactive_rather_than_failing(self):
        signal_set = a_set()
        assert not signal_set.is_active
        assert signal_set.inactive_reason
        assert signal_set.to_dict()["counts"]["total"] == 0


class TestGrouping:
    def test_signals_group_by_detector_and_pack(self):
        signal_set = a_set(
            decisions=[
                a_decision(feedbackId="fb_1", opportunityIdentity="opp_1"),
                a_decision(feedbackId="fb_2", opportunityIdentity="opp_2"),
                a_decision(
                    feedbackId="fb_3",
                    opportunityIdentity="opp_3",
                    detectorId="APPROVAL_BOTTLENECK",
                ),
            ]
        )
        groups = group_by_similarity(signal_set)
        assert len(groups) == 2

    def test_a_group_nets_accepts_against_dismisses(self):
        signal_set = a_set(
            decisions=[
                a_decision(feedbackId="fb_1", opportunityIdentity="opp_1"),
                a_decision(
                    feedbackId="fb_2", opportunityIdentity="opp_2", action="dismiss"
                ),
            ]
        )
        group = group_by_similarity(signal_set)[0]
        assert group.net_weight == pytest.approx(0.0, abs=1e-9)

    def test_a_group_separates_outcome_weight_from_decision_weight(self):
        """T2 must be able to say 'and one delivered measured improvement'."""
        signal_set = a_set(
            decisions=[a_decision(feedbackId="fb_1", opportunityIdentity="opp_1")],
            outcomes=[an_outcome(opportunityIdentity="opp_2")],
        )
        group = group_by_similarity(signal_set)[0]
        assert group.outcome_weight > 0
        assert group.decision_weight > 0
        assert group.has_outcome_evidence

    def test_contributing_refs_put_outcomes_first(self):
        """AC2's links, ordered as an explanation should read them."""
        signal_set = a_set(
            decisions=[a_decision(feedbackId="fb_1", opportunityIdentity="opp_1")],
            outcomes=[an_outcome(opportunityIdentity="opp_2")],
        )
        refs = group_by_similarity(signal_set)[0].contributing_refs
        assert refs[0]["kind"] == SOURCE_OUTCOME

    def test_grouping_is_deterministic(self):
        decisions = [
            a_decision(feedbackId=f"fb_{i}", opportunityIdentity=f"opp_{i}")
            for i in range(6)
        ]
        first = [g.key.to_dict() for g in group_by_similarity(a_set(decisions))]
        for _ in range(4):
            assert [
                g.key.to_dict() for g in group_by_similarity(a_set(decisions))
            ] == first

    def test_similar_to_excludes_the_findings_own_history(self):
        """Asking what SIMILAR findings say must not return the finding itself."""
        signal_set = a_set(
            decisions=[
                a_decision(feedbackId="fb_1", opportunityIdentity="opp_me"),
                a_decision(feedbackId="fb_2", opportunityIdentity="opp_other"),
            ]
        )
        key = similarity_key("HANDOFF_FRICTION", "service_cloud")
        matches = signal_set.similar_to(key, exclude_identity="opp_me")
        assert {s.opportunity_identity for s, _ in matches} == {"opp_other"}


class TestTheSetIsInspectable:
    def test_the_summary_is_json_serialisable(self):
        import json

        payload = describe_signal_set(
            a_set(
                decisions=[a_decision()],
                outcomes=[an_outcome(opportunityIdentity="opp_2")],
            )
        )
        assert json.loads(json.dumps(payload)) == payload

    def test_every_signal_reports_the_multipliers_that_produced_its_weight(self):
        """An unexplainable weight is not usable in an explainability feature."""
        signal = outcome_signal(an_outcome(), now=NOW, config=CONFIG)
        assert set(signal.multipliers) >= {"comparability", "confounders", "recency"}

    def test_the_summary_reports_its_cold_start_state_and_thresholds(self):
        payload = describe_signal_set(a_set(decisions=[a_decision()]))
        assert payload["isActive"] is False
        assert payload["inactiveReason"]
        assert payload["thresholds"]["minimumSignals"] == (
            CONFIG.cold_start.minimum_signals
        )

    def test_the_summary_reports_the_config_version_in_force(self):
        payload = describe_signal_set(a_set())
        assert payload["configVersion"] == CONFIG.config_version


class TestOrgIsolationAtTheModelLevel:
    def test_a_signal_set_carries_only_the_org_it_was_collected_for(self):
        """AC6 at this layer: the set is stamped with its org and never merged.

        The SQL-layer half of the guarantee is asserted structurally in
        test_learning_signal_isolation.py; this is the model-level half.
        """
        org_a = a_set(decisions=[a_decision(opportunityIdentity="opp_a")])
        org_b = collect_learning_signals(
            "org_2",
            now=NOW,
            config=CONFIG,
            decision_records=[a_decision(opportunityIdentity="opp_b")],
            outcome_records=[],
        )
        assert org_a.org_id == "org_1"
        assert org_b.org_id == "org_2"
        assert {s.opportunity_identity for s in org_a.signals} == {"opp_a"}
        assert {s.opportunity_identity for s in org_b.signals} == {"opp_b"}

    def test_a_malformed_record_is_skipped_rather_than_failing_the_set(self):
        signal_set = a_set(decisions=[a_decision(), None, "not a record", {}])
        assert len(signal_set.signals) == 1
