"""2.0-A3 T2 — the bounded adjustment layer.

Pure unit tests: :func:`adjust_ranking` takes its state and policy as arguments,
so the caps can be asserted directly with no database and no clock.

What is being protected, in order of importance:

* **the cap is real** — asserted over actual output, not over intent, because a
  cap that lives only in a config file is a cap that will drift;
* **base scoring is untouched and recoverable** — the layer annotates copies and
  never writes a score;
* **it narrows an existing order rather than replacing one** — everything the
  layer has no opinion about keeps its place.
"""

from __future__ import annotations

import random

import pytest

from app.learning_adjustment import (
    CAPPED_BY_RANK_MOVE,
    CAPPED_BY_SCORE_FRACTION,
    NOT_APPLIED_COLD_START,
    NOT_APPLIED_DISABLED,
    NOT_APPLIED_NO_STATE,
    RANKING_FIELD,
    GroupAdjustment,
    _bounded_placement,
    adjust_ranking,
    base_order,
)
from app.learning_signal_config import AdjustmentPolicy, load_config

CONFIG = load_config()
POLICY = CONFIG.adjustment


def opp(index: int, detector: str = "d_a", pack: str = "p", impact: float = 7.0):
    return {
        "id": f"opp_{index:03d}",
        "opportunity_identity": f"ident_{index:03d}",
        "title": f"Finding {index}",
        "impact": impact,
        "effort": 3,
        "tier": "Quick Win",
        "confidence": "HIGH",
        "corroboration_sources": ["servicenow"],
        "evidenceIds": [f"ev_{index}"],
        "packId": pack,
        "_debug": {"detector_id": detector},
    }


def opps(n: int, **kw):
    return [opp(i, **kw) for i in range(n)]


def group(net_weight: float, detector: str = "d_a", pack: str = "p", **kw):
    return {
        (detector, pack): GroupAdjustment(
            detector_id=detector, pack_id=pack, net_weight=net_weight, **kw
        )
    }


#: A deliberately tight policy for the cap fuzzing. The score fraction is opened
#: right up so the RANK cap is the only thing constraining movement — otherwise
#: the score cap would bind first and the rank cap would go untested.
_TIGHT = AdjustmentPolicy(
    max_score_fraction=1.0, max_rank_move=2, points_per_signal_unit=1.0
)


def _mixed_groups(seed: int, n: int = 14):
    """Findings across MANY groups with differing weights.

    One group with one weight gives every finding the same delta, and a uniform
    shift reorders nothing — which is how a cap test can pass while testing
    nothing. Differing weights are what produce the passive displacement the cap
    exists to bound.
    """
    random.seed(seed)
    items, state = [], {}
    for index in range(n):
        detector = f"d{index}"
        items.append(
            {
                "id": f"opp_{index:03d}",
                "impact": 10.0,
                "packId": "p",
                "_debug": {"detector_id": detector},
            }
        )
        state[(detector, "p")] = GroupAdjustment(
            detector, "p", net_weight=float(random.randint(-4, 4))
        )
    return items, state


# --------------------------------------------------------------------------
# The cap — the property the whole layer rests on.
# --------------------------------------------------------------------------


class TestTheCapIsRealNotDecorative:
    def test_a_plain_sort_would_not_have_honoured_the_rank_cap(self):
        """Why the layer does not simply sort by an adjusted key.

        Capping each item's delta at N still permits ACTUAL displacement of
        about 2N, because an item can be shoved by others jumping over it. A cap
        applied to the sort key but not to the outcome reads as enforced while
        allowing double the promised movement — so this test documents the
        failure the bounded placement exists to prevent.
        """
        random.seed(3)
        cap, n, worst = 2, 9, 0
        for _ in range(5000):
            deltas = [random.randint(-cap, cap) for _ in range(n)]
            order = sorted(range(n), key=lambda i: (i - deltas[i], i))
            worst = max(worst, max(abs(p - i) for p, i in enumerate(order)))
        assert worst > cap, (
            "the naive sort happened not to violate the cap in this sample; the "
            "guarantee still cannot be relied on, which is the point"
        )

    @pytest.mark.parametrize("cap", [0, 1, 2, 3, 5])
    def test_bounded_placement_never_exceeds_the_cap(self, cap):
        """Fuzzed over random keys — the bound holds by construction."""
        random.seed(cap + 17)
        for _ in range(3000):
            n = random.randint(1, 16)
            # The REAL key shape the layer builds: (target, -delta, base_index).
            # Fuzzing a simplified shape would leave the tie-break untested.
            deltas = [random.randint(-cap - 2, cap + 2) for _ in range(n)]
            keys = [(float(i) - deltas[i], -deltas[i], i) for i in range(n)]
            placement = _bounded_placement(keys, cap)
            assert placement is not None
            assert sorted(placement) == list(range(n)), "lost or duplicated an item"
            for slot, base_index in enumerate(placement):
                assert abs(slot - base_index) <= cap

    def test_bounded_placement_uses_rank_delta_as_target_tie_break(self):
        """A claimed target slot should beat its current incumbent."""

        keys = [
            (0.0, 0, 0),   # incumbent at slot 0
            (0.0, -1, 1),  # asks to move up into slot 0
        ]

        assert _bounded_placement(keys, max_move=1) == [1, 0]

    def test_no_finding_moves_further_than_the_rank_cap(self):
        """Asserted on real output from the real function, on data that BITES.

        The first version of this test put thirty findings in ONE group with one
        weight. Every item then received the same delta, a uniform shift moves
        nothing relative to anything, and the test passed just as happily with
        the bounded placement replaced by a plain sort — a cap assertion that
        never exercised the cap.

        Mixed groups with differing weights are what actually produce passive
        displacement, so that is what is fuzzed here.
        """
        for seed in range(150):
            items, state = _mixed_groups(seed)
            result = adjust_ranking(items, state, policy=_TIGHT)
            for record in result.adjustments:
                assert abs(record.moved) <= _TIGHT.max_rank_move, (
                    f"seed {seed}: {record.opportunity_id} moved {record.moved} "
                    f"places against a cap of {_TIGHT.max_rank_move}"
                )

    def test_the_same_data_breaks_the_cap_under_a_plain_sort(self):
        """The negative control, in the suite rather than in a scratch script.

        Proves the previous test is capable of failing: on this data a plain
        sort by the adjusted key — the obvious implementation — displaces a
        finding further than the cap allows.
        """
        worst = 0
        for seed in range(150):
            items, state = _mixed_groups(seed)
            keys = []
            for index, item in enumerate(items):
                group_state = state[(item["_debug"]["detector_id"], "p")]
                delta = max(
                    -_TIGHT.max_rank_move,
                    min(_TIGHT.max_rank_move, int(round(group_state.net_weight))),
                )
                keys.append((index - delta, index))
            plain = sorted(range(len(items)), key=lambda i: keys[i])
            worst = max(worst, max(abs(p - i) for p, i in enumerate(plain)))
        assert worst > _TIGHT.max_rank_move, (
            "the fuzz data no longer distinguishes bounded placement from a "
            "plain sort, so the cap test above has stopped proving anything"
        )

    def test_the_score_delta_never_exceeds_its_fraction_of_base_impact(self):
        """The mathematical bound, enforced where the delta is computed."""
        for base_impact in (1.0, 3.0, 7.0, 10.0):
            for weight in (-500.0, -8.0, -1.0, 1.0, 8.0, 500.0):
                result = adjust_ranking(
                    [opp(0, impact=base_impact)], group(weight), policy=POLICY
                )
                record = result.adjustments[0]
                assert abs(record.applied_delta) <= POLICY.score_cap_for(base_impact) + 1e-9

    def test_an_enormous_signal_cannot_move_a_finding_across_the_list(self):
        """AC1: adjustment stays within the cap however strong the signal."""
        items = opps(20)
        result = adjust_ranking(
            items,
            {("d_a", "p"): GroupAdjustment("d_a", "p", net_weight=1_000_000.0)},
            policy=POLICY,
        )
        assert result.max_movement <= POLICY.max_rank_move

    def test_a_zero_rank_cap_freezes_the_order_entirely(self):
        items = opps(8)
        frozen = AdjustmentPolicy(max_rank_move=0)
        result = adjust_ranking(items, group(50.0), policy=frozen)
        assert [o["id"] for o in result.ordered] == [o["id"] for o in items]
        assert result.adjustments
        served = result.ordered[0][RANKING_FIELD]
        assert served["adjusted"] is True
        assert served["moved"] == 0
        assert served["wasCapped"] is True
        assert served["reason"]["summary"]

    def test_the_caps_are_reported_on_every_served_finding(self):
        """A cap nobody can see is a cap nobody can hold you to."""
        result = adjust_ranking(opps(3), group(2.0), policy=POLICY)
        for served in result.ordered:
            caps = served[RANKING_FIELD]["caps"]
            assert caps["maxRankMove"] == POLICY.max_rank_move
            assert caps["maxScoreFraction"] == POLICY.max_score_fraction


# --------------------------------------------------------------------------
# Clipping is recorded, not silent.
# --------------------------------------------------------------------------


class TestClippingIsRecorded:
    def test_a_clipped_adjustment_says_it_was_clipped(self):
        """The interesting case: learning and the base scorer in tension.

        Recording it gives the tuning conversation something to work from, and
        matches A2's posture that a constrained result should say so.
        """
        result = adjust_ranking([opp(0, impact=9.0)], group(30.0), policy=POLICY)
        record = result.adjustments[0]
        assert record.was_capped
        assert record.capped_by in (CAPPED_BY_SCORE_FRACTION, CAPPED_BY_RANK_MOVE)
        assert abs(record.requested_delta) > abs(record.applied_delta)

    def test_the_record_carries_what_learning_actually_wanted(self):
        result = adjust_ranking([opp(0, impact=9.0)], group(30.0), policy=POLICY)
        record = result.adjustments[0]
        assert record.requested_delta == pytest.approx(
            30.0 * POLICY.points_per_signal_unit
        )

    def test_an_uncapped_adjustment_does_not_claim_to_be_capped(self):
        """A flag that is always set teaches a reader nothing."""
        result = adjust_ranking([opp(0, impact=10.0)], group(1.0), policy=POLICY)
        assert not result.adjustments[0].was_capped
        assert result.adjustments[0].capped_by is None

    def test_the_score_cap_is_named_when_it_is_the_binding_one(self):
        """A base impact of 1 gives a 0.15 budget — the score cap binds first."""
        result = adjust_ranking([opp(0, impact=1.0)], group(10.0), policy=POLICY)
        assert result.adjustments[0].capped_by == CAPPED_BY_SCORE_FRACTION

    def test_the_rank_cap_is_named_when_it_is_the_binding_one(self):
        """A generous score cap, a tight rank cap."""
        generous = AdjustmentPolicy(
            max_score_fraction=1.0, max_rank_move=1, points_per_signal_unit=1.0
        )
        result = adjust_ranking([opp(0, impact=10.0)], group(6.0), policy=generous)
        assert result.adjustments[0].capped_by == CAPPED_BY_RANK_MOVE

    def test_the_count_of_clipped_findings_is_reported(self):
        result = adjust_ranking(opps(6, impact=9.0), group(30.0), policy=POLICY)
        assert result.capped_count == 6


# --------------------------------------------------------------------------
# Base scoring untouched and recoverable — the defining property.
# --------------------------------------------------------------------------


class TestBaseScoringIsUntouched:
    def test_the_input_findings_are_not_mutated(self):
        items = opps(5)
        snapshot = [dict(o) for o in items]
        adjust_ranking(items, group(20.0), policy=POLICY)
        assert items == snapshot, "the layer wrote into the caller's findings"

    @pytest.mark.parametrize(
        "field", ["impact", "effort", "tier", "confidence", "evidenceIds",
                  "corroboration_sources"]
    )
    def test_no_scoring_or_evidence_field_is_changed(self, field):
        """AC3: never modifies evidence, confidence level, or corroboration."""
        items = opps(6)
        result = adjust_ranking(items, group(50.0), policy=POLICY)
        by_id = {o["id"]: o for o in items}
        for served in result.ordered:
            assert served[field] == by_id[served["id"]][field]

    def test_the_base_impact_travels_with_every_finding(self):
        result = adjust_ranking(opps(4, impact=6.0), group(3.0), policy=POLICY)
        for served in result.ordered:
            assert served[RANKING_FIELD]["baseImpact"] == 6.0

    def test_the_base_rank_travels_with_every_finding(self):
        """"What would this have ranked without learning?" — answerable inline."""
        items = opps(10)
        result = adjust_ranking(items, group(9.0), policy=POLICY)
        recovered = sorted(result.ordered, key=lambda o: o[RANKING_FIELD]["baseRank"])
        assert [o["id"] for o in recovered] == [o["id"] for o in items]

    def test_base_order_reconstructs_the_original_list(self):
        items = opps(7)
        assert [o["id"] for o in base_order(items)] == [o["id"] for o in items]

    def test_the_effective_score_is_derived_never_stored_over_the_base(self):
        result = adjust_ranking([opp(0, impact=7.0)], group(2.0), policy=POLICY)
        served = result.ordered[0]
        assert served["impact"] == 7.0, "the base score itself is untouched"
        assert served[RANKING_FIELD]["effectiveImpact"] != 7.0


# --------------------------------------------------------------------------
# It narrows an order; it does not replace one.
# --------------------------------------------------------------------------


class TestItNarrowsRatherThanReplaces:
    def test_no_adjustment_means_no_movement_at_all(self):
        items = opps(12)
        result = adjust_ranking(items, {}, policy=POLICY)
        assert [o["id"] for o in result.ordered] == [o["id"] for o in items]
        assert result.reason == NOT_APPLIED_NO_STATE

    def test_findings_the_layer_has_no_opinion_about_keep_their_order(self):
        """Only the learned component moves things."""
        items = opps(4, detector="d_a") + opps(4, detector="d_b")
        for index, item in enumerate(items):
            item["id"] = f"opp_{index:03d}"
        result = adjust_ranking(items, group(4.0, detector="d_a"), policy=POLICY)

        untouched = [o["id"] for o in result.ordered if o["_debug"]["detector_id"] == "d_b"]
        assert untouched == sorted(untouched), "unadjusted findings were reshuffled"

    def test_a_zero_weight_group_moves_nothing(self):
        items = opps(6)
        result = adjust_ranking(items, group(0.0), policy=POLICY)
        assert [o["id"] for o in result.ordered] == [o["id"] for o in items]

    def test_ordering_is_deterministic(self):
        items = opps(15)
        state = group(5.0)
        first = [o["id"] for o in adjust_ranking(items, state, policy=POLICY).ordered]
        for _ in range(5):
            assert [
                o["id"] for o in adjust_ranking(items, state, policy=POLICY).ordered
            ] == first

    def test_a_positive_signal_moves_a_finding_up(self):
        items = opps(8, detector="d_b")
        items[5] = opp(5, detector="d_a")
        result = adjust_ranking(items, group(6.0, detector="d_a"), policy=POLICY)
        moved = next(a for a in result.adjustments if a.opportunity_id == "opp_005")
        assert moved.moved < 0, "a favoured finding must move towards the top"

    def test_a_negative_signal_moves_a_finding_down(self):
        items = opps(8, detector="d_b")
        items[2] = opp(2, detector="d_a")
        result = adjust_ranking(items, group(-6.0, detector="d_a"), policy=POLICY)
        moved = next(a for a in result.adjustments if a.opportunity_id == "opp_002")
        assert moved.moved > 0


# --------------------------------------------------------------------------
# The gates.
# --------------------------------------------------------------------------


class TestTheGates:
    def test_cold_start_applies_nothing(self):
        """AC4: T1's gate is consulted, not re-implemented here."""
        items = opps(10)
        result = adjust_ranking(
            items, group(50.0), is_active=False,
            inactive_reason="Learning is not yet active: 3 of 10 recorded.",
            policy=POLICY,
        )
        assert not result.applied
        assert [o["id"] for o in result.ordered] == [o["id"] for o in items]
        assert "not yet active" in result.reason

    def test_the_cold_start_reason_is_carried_through_verbatim(self):
        result = adjust_ranking(
            opps(3), group(5.0), is_active=False, inactive_reason=None, policy=POLICY
        )
        assert result.reason == NOT_APPLIED_COLD_START

    def test_a_disabled_layer_serves_base_order_with_a_stated_reason(self):
        items = opps(9)
        off = AdjustmentPolicy(enabled=False)
        result = adjust_ranking(items, group(50.0), policy=off)
        assert result.reason == NOT_APPLIED_DISABLED
        assert [o["id"] for o in result.ordered] == [o["id"] for o in items]

    def test_an_unapplied_run_still_annotates_base_positions(self):
        """"Learning is off" must not mean "no base rank to show"."""
        result = adjust_ranking(opps(4), {}, policy=POLICY)
        for index, served in enumerate(result.ordered):
            assert served[RANKING_FIELD]["baseRank"] == index
            assert served[RANKING_FIELD]["adjusted"] is False

    def test_an_empty_list_is_handled(self):
        result = adjust_ranking([], group(5.0), policy=POLICY)
        assert result.ordered == ()
        assert not result.applied


# --------------------------------------------------------------------------
# Explainability groundwork (AC2 is T3; the data must exist now).
# --------------------------------------------------------------------------


class TestTheMovementIsExplainable:
    def test_an_adjusted_finding_carries_its_contributing_refs(self):
        refs = ({"kind": "outcome", "currentRunId": "run_9"},
                {"kind": "decision", "feedbackId": "fb_1"})
        state = {
            ("d_a", "p"): GroupAdjustment(
                "d_a", "p", net_weight=5.0, contributing_refs=refs, signal_count=2
            )
        }
        result = adjust_ranking(opps(3), state, policy=POLICY)
        assert result.adjustments[0].contributing_refs == refs

    def test_measured_evidence_is_flagged_separately_from_opinion(self):
        """So an explanation can say "and one delivered measured improvement"."""
        state = {
            ("d_a", "p"): GroupAdjustment(
                "d_a", "p", net_weight=5.0, has_outcome_evidence=True
            )
        }
        result = adjust_ranking(opps(3), state, policy=POLICY)
        assert result.adjustments[0].has_outcome_evidence

    def test_the_summary_is_json_serialisable(self):
        import json

        payload = adjust_ranking(opps(5), group(4.0), policy=POLICY).to_dict()
        assert json.loads(json.dumps(payload)) == payload

    def test_the_summary_reports_movement_and_capping_counts(self):
        payload = adjust_ranking(opps(6, impact=9.0), group(30.0), policy=POLICY).to_dict()
        assert payload["applied"] is True
        assert payload["maxMovement"] <= POLICY.max_rank_move
        assert payload["cappedCount"] == 6


# --------------------------------------------------------------------------
# Config refusals.
# --------------------------------------------------------------------------


class TestTheCapCannotBeConfiguredAway:
    def test_a_zero_score_fraction_is_refused(self):
        from app.learning_signal_config import (
            LearningConfigError,
            parse_config,
            validate_config,
        )

        raw = {
            "outcome_signals": {"within_band": {"weight": 3.0, "direction": "positive"}},
            "decision_signals": {"accept": {"weight": 1.0, "direction": "positive"}},
            "ranking_adjustment": {"max_score_fraction": 0.0},
        }
        with pytest.raises(LearningConfigError) as excinfo:
            validate_config(parse_config(raw))
        assert "max_score_fraction" in str(excinfo.value)

    def test_a_fraction_above_one_is_refused(self):
        from app.learning_signal_config import (
            LearningConfigError,
            parse_config,
            validate_config,
        )

        raw = {
            "outcome_signals": {"within_band": {"weight": 3.0, "direction": "positive"}},
            "decision_signals": {"accept": {"weight": 1.0, "direction": "positive"}},
            "ranking_adjustment": {"max_score_fraction": 1.5},
        }
        with pytest.raises(LearningConfigError) as excinfo:
            validate_config(parse_config(raw))
        assert "adjustment" in str(excinfo.value).lower()

    def test_the_shipped_config_carries_both_caps(self):
        assert 0 < POLICY.max_score_fraction <= 1.0
        assert POLICY.max_rank_move >= 0
        assert CONFIG.basis_for("ranking_adjustment")
