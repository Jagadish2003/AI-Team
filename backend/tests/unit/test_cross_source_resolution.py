"""2.0-B2 T1 — the ranked cross-source resolution engine.

Covers this subtask's acceptance criterion:

  AC1 — "Entities with explicit cross-references resolve automatically; entities
        matching only by name similarity are proposed, not merged."

Both halves are asserted directly, and the second one is additionally pinned
STRUCTURALLY: no combination of policy values, and no tier lookup, can ever turn
a name-similarity match into a merge. A convention that only holds because every
call site remembers it is not a guarantee — and a wrong merge is invisible once
it happens, which is why this AC is worth over-testing.

Also pinned here: the three tiers' ranking (a weaker tier never overrides a
stronger one), the four gates (org / entity type / self / resolution status),
ambiguity never merging, tier-3's corroboration requirement, and determinism.

DB-free throughout — the engine is pure by design, so these run without
PostgreSQL. The loader/tenancy half lives in
``tests/contract/test_cross_source_resolution_contract.py``.
"""
from __future__ import annotations

import itertools

from app import cross_source_resolution as csr
from app.entity_alias_mappings import build_alias_index, normalize_alias_mappings


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _entity(
    entity_id: str,
    display_name: str,
    *,
    source_system: str,
    source_record_id: str | None = None,
    org_id: str = "org_a",
    entity_type: str = "system",
    status: str = csr.STATUS_RESOLVED,
    metadata: dict | None = None,
) -> csr.ResolutionEntity:
    return csr.ResolutionEntity(
        entity_id=entity_id,
        org_id=org_id,
        entity_type=entity_type,
        display_name=display_name,
        canonical_name=" ".join(display_name.split()).lower(),
        source_system=source_system,
        source_record_id=source_record_id,
        resolution_status=status,
        cross_references=csr.extract_cross_references(
            metadata or {}, own_system=source_system
        ),
        metadata=metadata or {},
    )


def _edges(*pairs) -> csr.RelationshipIndex:
    """Build an observed-relationship index from (from, to, type) triples."""
    return csr.build_relationship_index(
        [
            {
                "from_entity_id": a,
                "to_entity_id": b,
                "relationship_type": rel,
                "inferred": False,
            }
            for a, b, rel in pairs
        ]
    )


# ─────────────────────────────────────────────────────────────────────────────
# AC1, first half — explicit cross-references resolve AUTOMATICALLY
# ─────────────────────────────────────────────────────────────────────────────


def test_ac1_subject_referencing_a_candidates_identity_auto_merges():
    """The ServiceNow entity cites the Jira record's own id — the source data
    states the identity, so no human is needed."""
    subject = _entity(
        "e1", "Payments Platform", source_system="servicenow", source_record_id="sn-1",
        metadata={"cross_references": [
            {"system": "jira", "record_id": "PAY", "field": "correlation_id"}
        ]},
    )
    target = _entity("e2", "Payments", source_system="jira", source_record_id="PAY")

    decision = csr.resolve_entity(subject, [target])

    assert decision.status == csr.STATUS_RESOLVED
    assert decision.is_merge is True
    assert decision.tier == csr.TIER_EXPLICIT_REFERENCE
    assert decision.merge_target is not None and decision.merge_target.entity_id == "e2"
    assert decision.confidence == csr.CONFIDENCE_EXPLICIT_REFERENCE
    assert decision.matches[0].action == csr.ACTION_MERGE
    # The merge names its evidence — which field, pointing at which identity.
    evidence = decision.matches[0].evidence
    assert evidence["match_kind"] == "subject_references_candidate"
    assert evidence["reference"]["field"] == "correlation_id"
    assert evidence["matched_identity"] == {"system": "jira", "record_id": "PAY"}


def test_ac1_reverse_direction_also_auto_merges():
    """Which side carries the reference is an accident of the connector."""
    subject = _entity("e1", "Payments", source_system="jira", source_record_id="PAY")
    target = _entity(
        "e2", "Payments Platform", source_system="servicenow", source_record_id="sn-1",
        metadata={"external_ids": {"jira": "PAY"}},
    )

    decision = csr.resolve_entity(subject, [target])

    assert decision.is_merge is True
    assert decision.matches[0].evidence["match_kind"] == "candidate_references_subject"


def test_ac1_a_shared_third_party_reference_auto_merges():
    """Neither cites the other, but both cite the SAME CMDB CI — still an
    explicit, machine-stated identity."""
    shared = {"cross_references": [{"system": "servicenow", "record_id": "ci-42"}]}
    subject = _entity("e1", "Payments API", source_system="jira",
                      source_record_id="PAY", metadata=shared)
    target = _entity("e2", "payments-api", source_system="git",
                     source_record_id="repo-9", metadata=shared)

    decision = csr.resolve_entity(subject, [target])

    assert decision.is_merge is True
    assert decision.tier == csr.TIER_EXPLICIT_REFERENCE
    assert decision.matches[0].evidence["shared_references"] == [
        {"system": "servicenow", "record_id": "ci-42"}
    ]


def test_ac1_alias_mapping_also_auto_merges():
    """Tier 2 is a HUMAN-stated identity — recorded, attributable, reversible —
    so it merges too. (The story's second auto-merge tier.)"""
    subject = _entity("e1", "Payments API", source_system="servicenow", source_record_id="sn-1")
    target = _entity("e2", "payments-api", source_system="git", source_record_id="repo-9")
    index = build_alias_index(normalize_alias_mappings([
        {"entity_type": "system", "canonical": "payments-api",
         "aliases": ["Payments API"], "created_by": "owner@example.com"}
    ]))

    decision = csr.resolve_entity(subject, [target], alias_index=index)

    assert decision.is_merge is True
    assert decision.tier == csr.TIER_ALIAS_MAPPING
    assert decision.confidence == csr.CONFIDENCE_ALIAS_MAPPING
    assert decision.matches[0].evidence["alias_group"] == "system:payments-api"
    assert decision.matches[0].evidence["created_by"] == "owner@example.com"


# ─────────────────────────────────────────────────────────────────────────────
# AC1, second half — name similarity is PROPOSED, never merged
# ─────────────────────────────────────────────────────────────────────────────


def test_ac1_name_only_match_is_proposed_never_merged():
    subject = _entity("e1", "Payments", source_system="servicenow", source_record_id="sn-1")
    target = _entity("e2", "payments", source_system="jira", source_record_id="PAY")
    # A corroborating observed relationship: both linked to the same team.
    rels = _edges(("e1", "team-1", "owns"), ("e2", "team-1", "owns"))

    decision = csr.resolve_entity(subject, [target], relationship_index=rels)

    assert decision.status == csr.STATUS_PROPOSED
    assert decision.is_merge is False, "a name match must never authorise a merge"
    assert decision.merge_target is None
    assert decision.tier == csr.TIER_NAME_SIMILARITY
    assert len(decision.proposals) == 1
    proposal = decision.proposals[0]
    assert proposal.action == csr.ACTION_PROPOSE
    assert proposal.target.entity_id == "e2"
    assert proposal.evidence["corroborating_relationships"] == [
        {"relationship_type": "owns", "entity_id": "team-1"}
    ]


def test_ac1_name_similarity_is_structurally_barred_from_merging():
    """Not a convention — a property of the tier table itself."""
    assert csr.TIER_NAME_SIMILARITY not in csr.AUTO_MERGE_TIERS
    assert csr.action_for_tier(csr.TIER_NAME_SIMILARITY) == csr.ACTION_PROPOSE
    assert csr.action_for_tier(csr.TIER_EXPLICIT_REFERENCE) == csr.ACTION_MERGE
    assert csr.action_for_tier(csr.TIER_ALIAS_MAPPING) == csr.ACTION_MERGE
    # Fail closed: an unknown/future tier can at most be proposed.
    assert csr.action_for_tier("some_future_tier") == csr.ACTION_PROPOSE


def test_ac1_no_policy_combination_can_merge_a_name_match():
    """Exhaustive over every policy permutation: the merge boundary is not a
    setting anyone can turn off."""
    subject = _entity("e1", "Payments", source_system="servicenow", source_record_id="sn-1")
    target = _entity("e2", "payments", source_system="jira", source_record_id="PAY")
    rels = _edges(("e1", "team-1", "owns"), ("e2", "team-1", "owns"))

    for corroborate, cross_source, max_proposals in itertools.product(
        (True, False), (True, False), (0, 1, 10)
    ):
        policy = csr.ResolutionPolicy(
            require_corroborating_relationship=corroborate,
            require_cross_source_for_name_tier=cross_source,
            max_proposals=max_proposals,
        )
        decision = csr.resolve_entity(
            subject, [target], relationship_index=rels, policy=policy
        )
        assert decision.is_merge is False, policy
        assert decision.merge_target is None, policy
        assert all(m.action != csr.ACTION_MERGE for m in decision.matches), policy


def test_a_name_match_without_corroboration_is_not_even_proposed():
    """The story's tier-3 signal is 'exact normalised name + corroborating
    relationship'. Without the second half every reused word ('admin', 'core')
    would flood the review queue — so it is recorded, not proposed."""
    subject = _entity("e1", "Core", source_system="servicenow", source_record_id="sn-1")
    target = _entity("e2", "core", source_system="jira", source_record_id="PAY")

    decision = csr.resolve_entity(subject, [target])

    assert decision.status == csr.STATUS_UNRESOLVED
    assert decision.proposals == ()
    skipped = decision.considered["name_matches_not_proposed"]
    assert skipped[0]["entity_id"] == "e2"
    assert skipped[0]["reason"] == csr.REASON_NO_CORROBORATION


def test_same_source_name_match_is_left_to_the_standing_engine():
    subject = _entity("e1", "Payments", source_system="servicenow", source_record_id="sn-1")
    target = _entity("e2", "payments", source_system="servicenow", source_record_id="sn-2")
    rels = _edges(("e1", "team-1", "owns"), ("e2", "team-1", "owns"))

    decision = csr.resolve_entity(subject, [target], relationship_index=rels)

    assert decision.status == csr.STATUS_UNRESOLVED
    assert decision.considered["name_matches_not_proposed"][0]["reason"] == csr.REASON_SAME_SOURCE


def test_inferred_edges_never_corroborate_a_proposal():
    """Corroborating a guess with a guess is not corroboration."""
    subject = _entity("e1", "Payments", source_system="servicenow", source_record_id="sn-1")
    target = _entity("e2", "payments", source_system="jira", source_record_id="PAY")
    index = csr.build_relationship_index([
        {"from_entity_id": "e1", "to_entity_id": "t", "relationship_type": "owns",
         "inferred": True},
        {"from_entity_id": "e2", "to_entity_id": "t", "relationship_type": "owns",
         "inferred": True},
    ])

    decision = csr.resolve_entity(subject, [target], relationship_index=index)

    assert decision.status == csr.STATUS_UNRESOLVED


def test_a_shared_neighbour_of_a_different_relationship_type_does_not_corroborate():
    subject = _entity("e1", "Payments", source_system="servicenow", source_record_id="sn-1")
    target = _entity("e2", "payments", source_system="jira", source_record_id="PAY")
    rels = _edges(("e1", "team-1", "owns"), ("e2", "team-1", "member_of"))

    decision = csr.resolve_entity(subject, [target], relationship_index=rels)

    assert decision.status == csr.STATUS_UNRESOLVED


# ─────────────────────────────────────────────────────────────────────────────
# Ranking — a weaker tier never overrides a stronger one
# ─────────────────────────────────────────────────────────────────────────────


def test_tiers_are_ranked_strongest_first():
    assert csr.TIERS_BY_RANK == (
        csr.TIER_EXPLICIT_REFERENCE,
        csr.TIER_ALIAS_MAPPING,
        csr.TIER_NAME_SIMILARITY,
    )
    assert (
        csr.TIER_RANK[csr.TIER_EXPLICIT_REFERENCE]
        < csr.TIER_RANK[csr.TIER_ALIAS_MAPPING]
        < csr.TIER_RANK[csr.TIER_NAME_SIMILARITY]
    )


def test_explicit_reference_wins_over_an_alias_mapping():
    """Both tiers point at different targets; the machine-stated fact wins."""
    subject = _entity(
        "e1", "Payments API", source_system="servicenow", source_record_id="sn-1",
        metadata={"external_ids": {"jira": "PAY"}},
    )
    referenced = _entity("e2", "Something Else", source_system="jira", source_record_id="PAY")
    aliased = _entity("e3", "payments-api", source_system="git", source_record_id="repo-9")
    index = build_alias_index(normalize_alias_mappings([
        {"entity_type": "system", "canonical": "payments-api", "aliases": ["Payments API"]}
    ]))

    decision = csr.resolve_entity(subject, [referenced, aliased], alias_index=index)

    assert decision.tier == csr.TIER_EXPLICIT_REFERENCE
    assert decision.merge_target.entity_id == "e2"


def test_alias_mapping_wins_over_a_name_proposal():
    subject = _entity("e1", "Payments API", source_system="servicenow", source_record_id="sn-1")
    aliased = _entity("e2", "payments-api", source_system="git", source_record_id="repo-9")
    same_name = _entity("e3", "payments api", source_system="jira", source_record_id="PAY")
    rels = _edges(("e1", "t", "owns"), ("e3", "t", "owns"))
    index = build_alias_index(normalize_alias_mappings([
        {"entity_type": "system", "canonical": "payments-api", "aliases": ["Payments API"]}
    ]))

    decision = csr.resolve_entity(
        subject, [aliased, same_name], alias_index=index, relationship_index=rels
    )

    assert decision.tier == csr.TIER_ALIAS_MAPPING
    assert decision.merge_target.entity_id == "e2"
    assert decision.proposals == (), "a merge decision does not also carry proposals"


# ─────────────────────────────────────────────────────────────────────────────
# Ambiguity never merges
# ─────────────────────────────────────────────────────────────────────────────


def test_two_explicit_reference_targets_are_ambiguous_and_never_merged():
    subject = _entity(
        "e1", "Payments", source_system="servicenow", source_record_id="sn-1",
        metadata={"cross_references": [
            {"system": "jira", "record_id": "PAY"},
            {"system": "git", "record_id": "repo-9"},
        ]},
    )
    a = _entity("e2", "Payments", source_system="jira", source_record_id="PAY")
    b = _entity("e3", "payments-api", source_system="git", source_record_id="repo-9")

    decision = csr.resolve_entity(subject, [a, b])

    assert decision.status == csr.STATUS_AMBIGUOUS
    assert decision.is_merge is False
    assert decision.merge_target is None
    assert decision.confidence == csr.CONFIDENCE_AMBIGUOUS
    # Every colliding candidate is recorded so a human can see what collided.
    assert {m.target.entity_id for m in decision.matches} == {"e2", "e3"}


def test_an_ambiguous_stronger_tier_does_not_fall_through_to_a_weaker_one():
    """Disagreeing explicit references are a source-data problem — not licence to
    merge on a weaker signal."""
    subject = _entity(
        "e1", "Payments", source_system="servicenow", source_record_id="sn-1",
        metadata={"cross_references": [
            {"system": "jira", "record_id": "PAY"},
            {"system": "git", "record_id": "repo-9"},
        ]},
    )
    a = _entity("e2", "Payments", source_system="jira", source_record_id="PAY")
    b = _entity("e3", "payments-api", source_system="git", source_record_id="repo-9")
    index = build_alias_index(normalize_alias_mappings([
        {"entity_type": "system", "canonical": "payments-api", "aliases": ["Payments"]}
    ]))

    decision = csr.resolve_entity(subject, [a, b], alias_index=index)

    assert decision.status == csr.STATUS_AMBIGUOUS
    assert decision.tier == csr.TIER_EXPLICIT_REFERENCE
    assert decision.merge_target is None


def test_two_alias_targets_are_ambiguous_and_never_merged():
    subject = _entity("e1", "Payments API", source_system="servicenow", source_record_id="sn-1")
    a = _entity("e2", "payments-api", source_system="git", source_record_id="repo-9")
    b = _entity("e3", "svc-payments", source_system="aws", source_record_id="arn-1")
    index = build_alias_index(normalize_alias_mappings([
        {"entity_type": "system", "canonical": "payments-api",
         "aliases": ["Payments API", "svc-payments"]}
    ]))

    decision = csr.resolve_entity(subject, [a, b], alias_index=index)

    assert decision.status == csr.STATUS_AMBIGUOUS
    assert decision.merge_target is None


# ─────────────────────────────────────────────────────────────────────────────
# Gates
# ─────────────────────────────────────────────────────────────────────────────


def test_a_cross_org_candidate_is_never_matched():
    """Identity must never cross a tenant boundary, even with a perfect
    reference."""
    subject = _entity(
        "e1", "Payments", source_system="servicenow", source_record_id="sn-1",
        metadata={"external_ids": {"jira": "PAY"}},
    )
    other_org = _entity(
        "e2", "Payments", source_system="jira", source_record_id="PAY", org_id="org_b"
    )

    decision = csr.resolve_entity(subject, [other_org])

    assert decision.status == csr.STATUS_UNRESOLVED
    assert decision.considered["dropped"]["cross_org"] == 1


def test_a_different_entity_type_is_never_matched():
    subject = _entity(
        "e1", "Payments", source_system="servicenow", source_record_id="sn-1",
        entity_type="system", metadata={"external_ids": {"jira": "PAY"}},
    )
    team = _entity(
        "e2", "Payments", source_system="jira", source_record_id="PAY", entity_type="team"
    )

    decision = csr.resolve_entity(subject, [team])

    assert decision.status == csr.STATUS_UNRESOLVED
    assert decision.considered["dropped"]["type_mismatch"] == 1


def test_an_entity_never_resolves_to_itself():
    subject = _entity(
        "e1", "Payments", source_system="servicenow", source_record_id="sn-1",
        metadata={"external_ids": {"jira": "PAY"}},
    )
    decision = csr.resolve_entity(subject, [subject])
    assert decision.status == csr.STATUS_UNRESOLVED
    assert decision.considered["dropped"]["self"] == 1


def test_an_ambiguous_candidate_row_is_never_a_merge_target():
    """The standing engine already recorded that it does not know what this row
    is; resolving onto it would launder that uncertainty into a merge."""
    subject = _entity(
        "e1", "Payments", source_system="servicenow", source_record_id="sn-1",
        metadata={"external_ids": {"jira": "PAY"}},
    )
    unsure = _entity(
        "e2", "Payments", source_system="jira", source_record_id="PAY", status="ambiguous"
    )

    decision = csr.resolve_entity(subject, [unsure])

    assert decision.status == csr.STATUS_UNRESOLVED
    assert decision.considered["dropped"]["not_resolved"] == 1


# ─────────────────────────────────────────────────────────────────────────────
# Cross-reference extraction — explicit only, never inferred
# ─────────────────────────────────────────────────────────────────────────────


def test_all_three_documented_reference_forms_are_read():
    """``cross_references`` (preferred), ``external_ids`` (map), and the
    enumerated single-field keys — read from a git entity, which is the realistic
    carrier of a CMDB CI id."""
    refs = csr.extract_cross_references(
        {
            "cross_references": [{"system": "jira", "record_id": "PAY", "field": "link"}],
            "external_ids": {"salesforce": "001xx"},
            "ci_sys_id": "ci-42",
        },
        own_system="git",
    )
    assert {(r.system, r.record_id) for r in refs} == {
        ("jira", "PAY"), ("salesforce", "001xx"), ("servicenow", "ci-42"),
    }
    # The single-field key names the field it came from, so a merge can cite it.
    by_system = {r.system: r for r in refs}
    assert by_system["servicenow"].field_name == "ci_sys_id"
    assert by_system["jira"].field_name == "link"


def test_an_unrecognised_metadata_key_is_never_read_as_a_reference():
    """A tier-1 match auto-merges, so a reference is never inferred from a field
    that merely looks like an id."""
    refs = csr.extract_cross_references(
        {"some_other_id": "PAY", "correlation_id": "PAY", "record": "PAY"},
        own_system="servicenow",
    )
    assert refs == ()


def test_a_self_referencing_id_is_dropped():
    """Otherwise an entity would match every sibling from its own source that
    happens to share a field value."""
    refs = csr.extract_cross_references(
        {"external_ids": {"servicenow": "sn-9", "jira": "PAY"}}, own_system="servicenow"
    )
    assert [r.system for r in refs] == ["jira"]


def test_reference_extraction_is_deterministic_and_deduplicated():
    metadata = {
        "cross_references": [
            {"system": "Jira", "record_id": "PAY"},
            {"system": "jira", "record_id": "PAY"},
        ],
        "external_ids": {"jira": "PAY", "git": "repo-9"},
    }
    first = csr.extract_cross_references(metadata, own_system="servicenow")
    second = csr.extract_cross_references(metadata, own_system="servicenow")
    assert first == second
    assert [(r.system, r.record_id) for r in first] == [("git", "repo-9"), ("jira", "PAY")]


def test_an_entity_without_a_source_record_id_is_never_a_tier_one_target():
    """A name-derived entity has no stable identity to reference."""
    subject = _entity(
        "e1", "Payments", source_system="servicenow", source_record_id="sn-1",
        metadata={"external_ids": {"jira": "PAY"}},
    )
    nameless = _entity("e2", "Payments", source_system="jira", source_record_id=None)

    decision = csr.resolve_entity(subject, [nameless])

    assert decision.status == csr.STATUS_UNRESOLVED


# ─────────────────────────────────────────────────────────────────────────────
# Batch behaviour + determinism
# ─────────────────────────────────────────────────────────────────────────────


def test_batch_resolution_is_deterministic_and_independent():
    a = _entity("e1", "Payments", source_system="servicenow", source_record_id="sn-1",
                metadata={"external_ids": {"jira": "PAY"}})
    b = _entity("e2", "Payments", source_system="jira", source_record_id="PAY")
    c = _entity("e3", "Billing", source_system="git", source_record_id="repo-1")
    pool = [a, b, c]

    first = [d.to_dict() for d in csr.resolve_entities(pool, pool)]
    second = [d.to_dict() for d in csr.resolve_entities(list(reversed(pool)), pool)]

    by_subject = {d["subject"]["entity_id"]: d for d in first}
    by_subject_rev = {d["subject"]["entity_id"]: d for d in second}
    assert by_subject == by_subject_rev, "a subject's outcome must not depend on batch order"
    assert by_subject["e1"]["status"] == csr.STATUS_RESOLVED
    assert by_subject["e2"]["status"] == csr.STATUS_RESOLVED  # reverse reference
    assert by_subject["e3"]["status"] == csr.STATUS_UNRESOLVED


def test_merge_and_proposal_decisions_are_separable():
    merger = _entity("e1", "Payments", source_system="servicenow", source_record_id="sn-1",
                     metadata={"external_ids": {"jira": "PAY"}})
    target = _entity("e2", "Payments", source_system="jira", source_record_id="PAY")
    proposer = _entity("e3", "Billing", source_system="servicenow", source_record_id="sn-2")
    proposal_target = _entity("e4", "billing", source_system="git", source_record_id="repo-1")
    rels = _edges(("e3", "t", "owns"), ("e4", "t", "owns"))
    pool = [merger, target, proposer, proposal_target]

    decisions = csr.resolve_entities(pool, pool, relationship_index=rels)

    merges = {d.subject.entity_id for d in csr.merge_decisions(decisions)}
    proposals = {d.subject.entity_id for d in csr.proposal_decisions(decisions)}
    assert merges == {"e1", "e2"}
    assert proposals == {"e3", "e4"}
    assert not (merges & proposals), "a decision is either a merge or a proposal"


def test_max_proposals_caps_the_review_queue_and_records_the_drop():
    subject = _entity("e0", "Core", source_system="servicenow", source_record_id="sn-0")
    targets = [
        _entity(f"e{i}", "core", source_system=f"src{i}", source_record_id=f"r{i}")
        for i in range(1, 5)
    ]
    rels = _edges(*[("e0", "t", "owns")] + [(f"e{i}", "t", "owns") for i in range(1, 5)])

    decision = csr.resolve_entity(
        subject, targets, relationship_index=rels,
        policy=csr.ResolutionPolicy(max_proposals=2),
    )

    assert len(decision.proposals) == 2
    dropped = [
        s for s in decision.considered["name_matches_not_proposed"]
        if "max_proposals" in s["reason"]
    ]
    assert len(dropped) == 2, "a truncated review queue is stated, never silent"


def test_no_candidates_is_unresolved_not_an_error():
    subject = _entity("e1", "Payments", source_system="servicenow", source_record_id="sn-1")
    decision = csr.resolve_entity(subject, [])
    assert decision.status == csr.STATUS_UNRESOLVED
    assert decision.considered["eligible"] == 0


def test_decision_serialises_for_a_downstream_consumer():
    subject = _entity("e1", "Payments", source_system="servicenow", source_record_id="sn-1",
                      metadata={"external_ids": {"jira": "PAY"}})
    target = _entity("e2", "Payments", source_system="jira", source_record_id="PAY")

    payload = csr.resolve_entity(subject, [target]).to_dict()

    assert payload["status"] == csr.STATUS_RESOLVED
    assert payload["merge_target"]["entity_id"] == "e2"
    assert payload["matches"][0]["tier"] == csr.TIER_EXPLICIT_REFERENCE
    assert payload["proposals"] == []


# ─────────────────────────────────────────────────────────────────────────────
# Agreement with the standing entity-resolution engine
# ─────────────────────────────────────────────────────────────────────────────


def test_canonicalisation_matches_the_standing_engine():
    """If the two layers disagreed about what a name is, they would disagree
    about identity — the failure mode that produces a wrong merge."""
    from app.entity_resolution import canonical_name_for

    for raw in ("Payments  API", " payments api ", "PAYMENTS API"):
        assert canonical_name_for(raw) == "payments api"


def test_resolution_entity_is_built_from_a_persisted_row():
    from database.models.entities import Entity

    entity = Entity(
        org_id="org_a",
        entity_type="system",
        canonical_name="payments api",
        display_name="Payments  API",
        source_system="servicenow",
        source_record_id="sn-1",
        resolution_confidence=1.0,
        resolution_status="resolved",
        first_seen_run_id="run_1",
        last_seen_run_id="run_1",
        metadata={"external_ids": {"jira": "PAY"}},
    )

    view = csr.resolution_entity_from_entity(entity)

    assert view.canonical_name == "payments api"
    assert view.org_id == "org_a"
    assert [(r.system, r.record_id) for r in view.cross_references] == [("jira", "PAY")]


def test_the_engine_writes_nothing(monkeypatch):
    """T1 DECIDES; applying a merge is a later task. A resolver that quietly
    wrote would make that boundary meaningless."""
    from app import db

    def _boom(*_a, **_k):
        raise AssertionError("the resolution engine must not touch the database")

    monkeypatch.setattr(db, "connect", _boom)
    subject = _entity("e1", "Payments", source_system="servicenow", source_record_id="sn-1",
                      metadata={"external_ids": {"jira": "PAY"}})
    target = _entity("e2", "Payments", source_system="jira", source_record_id="PAY")

    assert csr.resolve_entity(subject, [target]).is_merge is True
