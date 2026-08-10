"""Tier 4 — leading-word name matching, opt-in and proposal-only.

Why the tier exists: some deployments cannot align names across systems. A
ServiceNow assignment group called "Payment Escalations" and a Salesforce queue
called "Payment Operations" may be named by real organisational history, and
renaming either is not available to the people who need the pair reviewed. Tier 3
requires exact equality, so those pairs are invisible to it.

Why it is a SEPARATE tier rather than a loosening of tier 3: tier 3's exactness is
depended on elsewhere — most importantly ``corroboration_identity_gate``, which
consults a tier-3 re-derivation before letting a finding reach HIGH. Widening tier
3 would let a finding be elevated on a shared first word. These tests pin that
separation, and pin that the new tier cannot merge under any configuration.

The other half of what is tested here is COST. Leading-word matching is
combinatorial — measured on one real org, "case" appears in 61 entity names, which
is 1,830 candidate pairs. So the corroborating-relationship requirement and the
proposal cap must still bite, and the default must be off.
"""
from __future__ import annotations

import itertools

import pytest

from app.cross_source_resolution import (
    ACTION_MERGE,
    ACTION_PROPOSE,
    AUTO_MERGE_TIERS,
    CONFIDENCE_NAME_PREFIX,
    CONFIDENCE_NAME_SIMILARITY,
    DEFAULT_POLICY,
    STATUS_PROPOSED,
    STATUS_UNRESOLVED,
    TIER_NAME_PREFIX,
    TIER_NAME_SIMILARITY,
    TIER_RANK,
    ResolutionEntity,
    ResolutionPolicy,
    RelationshipIndex,
    action_for_tier,
    resolve_entity,
)


def entity(eid, name, source, etype="team"):
    return ResolutionEntity(
        entity_id=eid,
        org_id="org-a",
        entity_type=etype,
        canonical_name=name,
        display_name=name.title(),
        source_system=source,
        source_record_id=None,
        resolution_status="resolved",
        metadata={},
    )


SF = entity("e-sf", "payment operations", "salesforce")
SN = entity("e-sn", "payment escalations", "servicenow")


def index(*pairs):
    """A real RelationshipIndex in which each given pair shares one neighbour.

    Built through the production class rather than a stub, so the corroboration
    semantics under test are the ones the engine actually uses — including that the
    neighbour key carries the relationship TYPE.
    """
    neighbours: dict = {}
    shared_edge = ("routes_to", "e-shared")
    for a, b in pairs:
        neighbours.setdefault(a, set()).add(shared_edge)
        neighbours.setdefault(b, set()).add(shared_edge)
    return RelationshipIndex(
        neighbours={k: frozenset(v) for k, v in neighbours.items()}
    )


def opted_in(**kw):
    return ResolutionPolicy(name_prefix_words=1, **kw)


# ---------------------------------------------------------------------------
# Off by default
# ---------------------------------------------------------------------------

class TestOffByDefault:
    def test_the_default_policy_disables_the_tier(self):
        assert DEFAULT_POLICY.name_prefix_words == 0

    def test_a_leading_word_pair_is_not_proposed_by_default(self):
        """Every existing deployment must behave exactly as before."""
        decision = resolve_entity(
            SF, [SN], relationship_index=index(("e-sf", "e-sn"))
        )
        assert decision.status == STATUS_UNRESOLVED
        assert decision.proposals == ()

    def test_zero_and_negative_both_disable(self):
        for words in (0, -1, -5):
            decision = resolve_entity(
                SF, [SN],
                policy=ResolutionPolicy(name_prefix_words=words),
                relationship_index=index(("e-sf", "e-sn")),
            )
            assert decision.status == STATUS_UNRESOLVED, words


# ---------------------------------------------------------------------------
# What it does when switched on
# ---------------------------------------------------------------------------

class TestOptedIn:
    def test_the_motivating_pair_is_proposed(self):
        decision = resolve_entity(
            SF, [SN], policy=opted_in(), relationship_index=index(("e-sf", "e-sn"))
        )
        assert decision.status == STATUS_PROPOSED
        assert decision.tier == TIER_NAME_PREFIX
        assert [m.target.entity_id for m in decision.proposals] == ["e-sn"]

    def test_it_proposes_and_never_merges(self):
        decision = resolve_entity(
            SF, [SN], policy=opted_in(), relationship_index=index(("e-sf", "e-sn"))
        )
        assert decision.merge_target is None
        assert all(m.action == ACTION_PROPOSE for m in decision.proposals)
        assert not any(m.is_merge for m in decision.proposals)

    def test_the_reason_says_the_full_names_differ(self):
        """A reviewer must not read this as an exact match."""
        decision = resolve_entity(
            SF, [SN], policy=opted_in(), relationship_index=index(("e-sf", "e-sn"))
        )
        reason = decision.proposals[0].reason
        assert "full names differ" in reason.lower()
        assert "payment" in reason

    def test_the_evidence_carries_both_full_names(self):
        decision = resolve_entity(
            SF, [SN], policy=opted_in(), relationship_index=index(("e-sf", "e-sn"))
        )
        ev = decision.proposals[0].evidence
        assert ev["matched_prefix"] == "payment"
        assert {ev["subject_name"], ev["target_name"]} == {
            "payment operations", "payment escalations",
        }

    def test_corroboration_evidence_matches_the_shape_the_ui_reads(self):
        """The UI renders {relationship_type, entity_id}. Handing it the engine's
        raw (type, id) tuples rendered a bare "Both" with both values blank on
        every card — pinned here so the two tiers cannot diverge again."""
        decision = resolve_entity(
            SF, [SN], policy=opted_in(), relationship_index=index(("e-sf", "e-sn"))
        )
        rels = decision.proposals[0].evidence["corroborating_relationships"]
        assert rels and all(isinstance(r, dict) for r in rels)
        assert set(rels[0]) == {"relationship_type", "entity_id"}
        assert rels[0]["relationship_type"] == "routes_to"
        assert rels[0]["entity_id"] == "e-shared"

    def test_confidence_is_strictly_below_an_exact_match(self):
        assert CONFIDENCE_NAME_PREFIX < CONFIDENCE_NAME_SIMILARITY

    def test_two_words_can_be_required_instead_of_one(self):
        near = entity("e-x", "payment operations europe", "servicenow")
        far = entity("e-y", "payment escalations", "servicenow")
        decision = resolve_entity(
            SF, [near, far],
            policy=ResolutionPolicy(name_prefix_words=2),
            relationship_index=index(("e-sf", "e-x"), ("e-sf", "e-y")),
        )
        # 'payment operations' matches the first two words of near, not of far.
        assert [m.target.entity_id for m in decision.proposals] == ["e-x"]


# ---------------------------------------------------------------------------
# The merge boundary — the property that must hold under every configuration
# ---------------------------------------------------------------------------

class TestTheMergeBoundaryHolds:
    def test_the_tier_is_absent_from_the_auto_merge_set(self):
        assert TIER_NAME_PREFIX not in AUTO_MERGE_TIERS

    def test_action_for_tier_can_only_propose_it(self):
        assert action_for_tier(TIER_NAME_PREFIX) == ACTION_PROPOSE

    def test_no_policy_permutation_produces_a_merge(self):
        """The sweep tier 3 already has, extended to tier 4. If any combination of
        knobs could merge on a partial name, the guarantee is a convention rather
        than a structure."""
        for corrob, cross, words, cap in itertools.product(
            (True, False), (True, False), (0, 1, 2, 3), (0, 1, 10)
        ):
            decision = resolve_entity(
                SF, [SN],
                policy=ResolutionPolicy(
                    require_corroborating_relationship=corrob,
                    require_cross_source_for_name_tier=cross,
                    name_prefix_words=words,
                    max_proposals=cap,
                ),
                relationship_index=index(("e-sf", "e-sn")),
            )
            assert decision.merge_target is None, (corrob, cross, words, cap)
            assert not any(
                m.action == ACTION_MERGE for m in decision.matches
            ), (corrob, cross, words, cap)

    def test_entity_merge_has_no_rule_for_this_tier(self):
        """The second, independent gate: even reached directly, the merge applier
        refuses a tier it has no rule for."""
        from app.entity_merge import _RULE_FOR_TIER

        assert TIER_NAME_PREFIX not in _RULE_FOR_TIER

    def test_the_identity_gate_does_not_accept_this_tier(self):
        """2.0-B2 T6: a finding must not reach HIGH on a partial name match."""
        from app.corroboration_identity_gate import RESOLVED_BASES

        assert not any(TIER_NAME_PREFIX in str(b) for b in RESOLVED_BASES)


# ---------------------------------------------------------------------------
# Tier 3 keeps priority, and keeps its meaning
# ---------------------------------------------------------------------------

class TestTierThreeIsUnchanged:
    def test_an_exact_match_wins_over_a_partial_one(self):
        exact = entity("e-exact", "payment operations", "servicenow")
        decision = resolve_entity(
            SF, [exact, SN],
            policy=opted_in(),
            relationship_index=index(("e-sf", "e-exact"), ("e-sf", "e-sn")),
        )
        assert decision.tier == TIER_NAME_SIMILARITY
        assert [m.target.entity_id for m in decision.proposals] == ["e-exact"]

    def test_an_exact_pair_is_never_re_proposed_as_partial(self):
        exact = entity("e-exact", "payment operations", "servicenow")
        decision = resolve_entity(
            SF, [exact], policy=opted_in(),
            relationship_index=index(("e-sf", "e-exact")),
        )
        assert decision.tier == TIER_NAME_SIMILARITY

    def test_tier_four_ranks_weakest(self):
        assert TIER_RANK[TIER_NAME_PREFIX] > TIER_RANK[TIER_NAME_SIMILARITY]


# ---------------------------------------------------------------------------
# Cost control — the guards that keep the queue answerable
# ---------------------------------------------------------------------------

class TestCostControl:
    def test_a_pair_with_no_shared_neighbour_is_not_proposed(self):
        """Without this, every reused word in two systems floods the queue."""
        decision = resolve_entity(SF, [SN], policy=opted_in(), relationship_index=index())
        assert decision.status == STATUS_UNRESOLVED
        assert decision.considered["prefix_matches_not_proposed"][0]["reason"]

    def test_same_source_pairs_are_skipped_and_counted(self):
        same = entity("e-sf2", "payment escalations", "salesforce")
        decision = resolve_entity(
            SF, [same], policy=opted_in(), relationship_index=index(("e-sf", "e-sf2"))
        )
        assert decision.status == STATUS_UNRESOLVED
        assert decision.considered["prefix_matches_not_proposed"]

    def test_a_different_entity_type_never_matches(self):
        other = entity("e-sys", "payment escalations", "servicenow", etype="system")
        decision = resolve_entity(
            SF, [other], policy=opted_in(), relationship_index=index(("e-sf", "e-sys"))
        )
        assert decision.status == STATUS_UNRESOLVED

    def test_proposals_are_capped(self):
        others = [entity(f"e-{i}", f"payment thing {i}", "servicenow") for i in range(9)]
        decision = resolve_entity(
            SF, others,
            policy=ResolutionPolicy(name_prefix_words=1, max_proposals=3),
            relationship_index=index(*[("e-sf", o.entity_id) for o in others]),
        )
        assert len(decision.proposals) == 3

    def test_a_one_word_name_does_match_on_one_word(self):
        """"Payment" against "Payment Escalations" is a real question — a reviewer
        may well answer yes. Suppressing it would be an arbitrary carve-out; the
        volume it invites is held down by corroboration and the cap instead."""
        one = entity("e-one", "payment", "salesforce")
        decision = resolve_entity(
            one, [SN], policy=opted_in(), relationship_index=index(("e-one", "e-sn"))
        )
        assert decision.status == STATUS_PROPOSED
        assert decision.tier == TIER_NAME_PREFIX

    def test_a_shorter_candidate_name_never_matches_a_longer_prefix(self):
        one = entity("e-one", "payment", "servicenow")
        decision = resolve_entity(
            SF, [one],
            policy=ResolutionPolicy(name_prefix_words=2),
            relationship_index=index(("e-sf", "e-one")),
        )
        assert decision.status == STATUS_UNRESOLVED


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------

def test_the_decision_is_order_independent():
    others = [entity(f"e-{i}", f"payment case {i}", "servicenow") for i in range(4)]
    idx = index(*[("e-sf", o.entity_id) for o in others])
    forward = resolve_entity(SF, others, policy=opted_in(), relationship_index=idx)
    reverse = resolve_entity(
        SF, list(reversed(others)), policy=opted_in(), relationship_index=idx
    )
    assert [m.target.entity_id for m in forward.proposals] == [
        m.target.entity_id for m in reverse.proposals
    ]
