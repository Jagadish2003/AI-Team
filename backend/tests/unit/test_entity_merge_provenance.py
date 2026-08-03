"""2.0-B2 T2 — merged-entity provenance (DB-free).

AC2: "A resolved entity exposes all constituent source identities and the rule
that resolved it."

A merge is the most destructive-by-accident operation in the platform, so the
parts that decide WHAT gets written are pinned here, without a database:

  * the constituent list is COMPLETE — it includes the survivor's own identity,
    or the node cannot honestly say which systems it speaks for;
  * the rule is recorded PER CONSTITUENT — a node merged by three rules on three
    days cannot be described by one rule field;
  * an earlier rule is never rewritten by a later merge;
  * survivor selection is deterministic, so a re-run never rewrites history;
  * a chain of merges loses no identity in the middle;
  * re-folding the same constituent is a no-op (what makes apply idempotent).

The SQL half (transitivity through ``merged_into``, idempotency, tenancy, the
audit row, exposure through the entity API) is in
``tests/contract/test_entity_merge_contract.py``.
"""
from __future__ import annotations

import json
from typing import Any, Dict

import pytest

from app import entity_merge as em


def _row(
    entity_id: str,
    *,
    name: str,
    source: str,
    record_id: str | None = "r1",
    created_at: str = "2026-01-01T00:00:00+00:00",
    metadata: Dict[str, Any] | None = None,
    entity_type: str = "system",
) -> Dict[str, Any]:
    return {
        "id": entity_id,
        "org_id": "org_a",
        "entity_type": entity_type,
        "display_name": name,
        "canonical_name": " ".join(name.split()).lower(),
        "source_system": source,
        "source_record_id": record_id,
        "created_at": created_at,
        "metadata": json.dumps(metadata) if metadata is not None else None,
    }


# ── the constituent list is complete ────────────────────────────────────────


def test_the_survivors_own_identity_is_in_the_list():
    """A merged node's identity list must be the COMPLETE set of things it is.
    Omitting the survivor's own identity would make it look like it came only
    from the source it absorbed."""
    survivor = _row("e1", name="Payments Platform", source="servicenow", record_id="sn-1")
    incoming = _row("e2", name="Payments", source="jira", record_id="PAY")

    provenance = em.build_merged_provenance(
        survivor, incoming, rule=em.RULE_EXPLICIT_REFERENCE
    )

    by_id = {c.entity_id: c for c in provenance.constituents}
    assert set(by_id) == {"e1", "e2"}
    assert by_id["e1"].is_origin is True
    assert by_id["e1"].source_system == "servicenow"
    assert by_id["e1"].source_record_id == "sn-1"
    assert by_id["e2"].is_origin is False
    assert by_id["e2"].source_system == "jira"
    assert by_id["e2"].source_record_id == "PAY"
    assert provenance.source_systems == ("jira", "servicenow")


def test_the_origin_is_never_stamped_with_a_rule():
    """The survivor was not merged in — it is what the others were merged INTO."""
    provenance = em.build_merged_provenance(
        _row("e1", name="A", source="servicenow"),
        _row("e2", name="A", source="jira"),
        rule=em.RULE_ALIAS_MAPPING,
    )
    origin = next(c for c in provenance.constituents if c.is_origin)
    assert origin.rule is None
    assert origin.merged_at is None


def test_source_identities_are_exposed_as_system_and_record():
    provenance = em.build_merged_provenance(
        _row("e1", name="A", source="servicenow", record_id="sn-1"),
        _row("e2", name="A", source="git", record_id="repo-1"),
        rule=em.RULE_EXPLICIT_REFERENCE,
    )
    assert sorted(provenance.source_identities, key=lambda i: i["source_system"]) == [
        {"source_system": "git", "source_record_id": "repo-1"},
        {"source_system": "servicenow", "source_record_id": "sn-1"},
    ]


def test_an_entity_with_no_stable_record_id_still_contributes_its_identity():
    provenance = em.build_merged_provenance(
        _row("e1", name="A", source="servicenow", record_id="sn-1"),
        _row("e2", name="A", source="slack", record_id=None),
        rule=em.RULE_ALIAS_MAPPING,
    )
    absorbed = next(c for c in provenance.constituents if c.entity_id == "e2")
    assert absorbed.source_system == "slack"
    assert absorbed.source_record_id is None


# ── the rule is recorded, per constituent ───────────────────────────────────


@pytest.mark.parametrize("rule", list(em.MERGE_RULES))
def test_every_valid_rule_is_recorded_on_the_constituent(rule):
    provenance = em.build_merged_provenance(
        _row("e1", name="A", source="servicenow"),
        _row("e2", name="A", source="jira"),
        rule=rule,
        actor="analyst-1",
        confidence=0.95,
    )
    absorbed = next(c for c in provenance.constituents if c.entity_id == "e2")
    assert absorbed.rule == rule
    assert absorbed.merged_by == "analyst-1"
    assert absorbed.confidence == 0.95
    assert absorbed.merged_at
    assert provenance.rules == (rule,)


def test_an_unknown_rule_is_refused():
    """A merge whose rule cannot be named is a merge nobody can explain."""
    with pytest.raises(em.EntityMergeError, match="rule must be one of"):
        em.build_merged_provenance(
            _row("e1", name="A", source="servicenow"),
            _row("e2", name="A", source="jira"),
            rule="looked_similar",
        )


def test_a_confirmed_proposal_is_its_own_rule():
    """A name match never authorises a merge — the person who confirmed it did,
    and the provenance must say that rather than crediting the tier."""
    assert em.RULE_CONFIRMED_PROPOSAL in em.MERGE_RULES
    assert "name_similarity" not in em.MERGE_RULES


def test_two_rules_on_one_entity_are_both_kept():
    """A node merged by an explicit reference and later by a human confirmation
    was produced by BOTH — one rule field would have to lie about one of them."""
    first = em.build_merged_provenance(
        _row("e1", name="A", source="servicenow"),
        _row("e2", name="A", source="jira"),
        rule=em.RULE_EXPLICIT_REFERENCE,
    )
    survivor = _row(
        "e1", name="A", source="servicenow",
        metadata={em.METADATA_MERGE_PROVENANCE: first.to_dict()},
    )
    second = em.build_merged_provenance(
        survivor,
        _row("e3", name="A", source="git"),
        rule=em.RULE_CONFIRMED_PROPOSAL,
        actor="analyst-1",
    )

    assert second.rules == (em.RULE_CONFIRMED_PROPOSAL, em.RULE_EXPLICIT_REFERENCE)
    by_id = {c.entity_id: c for c in second.constituents}
    assert by_id["e2"].rule == em.RULE_EXPLICIT_REFERENCE
    assert by_id["e3"].rule == em.RULE_CONFIRMED_PROPOSAL
    assert by_id["e3"].merged_by == "analyst-1"


def test_a_later_merge_never_rewrites_an_earlier_rule():
    """Re-attributing an old decision to today's rule would misreport who decided
    what — the single most misleading thing provenance could do."""
    first = em.build_merged_provenance(
        _row("e1", name="A", source="servicenow"),
        _row("e2", name="A", source="jira"),
        rule=em.RULE_EXPLICIT_REFERENCE,
        actor="system",
        merged_at="2026-01-02T00:00:00+00:00",
    )
    survivor = _row(
        "e1", name="A", source="servicenow",
        metadata={em.METADATA_MERGE_PROVENANCE: first.to_dict()},
    )
    second = em.build_merged_provenance(
        survivor, _row("e9", name="A", source="git"),
        rule=em.RULE_ALIAS_MAPPING, actor="owner-1",
        merged_at="2026-06-01T00:00:00+00:00",
    )
    original = next(c for c in second.constituents if c.entity_id == "e2")
    assert original.rule == em.RULE_EXPLICIT_REFERENCE
    assert original.merged_by == "system"
    assert original.merged_at == "2026-01-02T00:00:00+00:00"


# ── chains keep every identity ──────────────────────────────────────────────


def test_merging_an_already_merged_entity_keeps_the_identities_in_the_middle():
    """B absorbed C; A then absorbs B. A must speak for all three, or the middle
    identity vanishes from the graph silently."""
    b_provenance = em.build_merged_provenance(
        _row("b", name="A", source="jira", record_id="PAY"),
        _row("c", name="A", source="git", record_id="repo-1"),
        rule=em.RULE_ALIAS_MAPPING,
    )
    merged_b = _row(
        "b", name="A", source="jira", record_id="PAY",
        metadata={em.METADATA_MERGE_PROVENANCE: b_provenance.to_dict()},
    )

    final = em.build_merged_provenance(
        _row("a", name="A", source="servicenow", record_id="sn-1"),
        merged_b,
        rule=em.RULE_EXPLICIT_REFERENCE,
    )

    assert {c.entity_id for c in final.constituents} == {"a", "b", "c"}
    assert final.source_systems == ("git", "jira", "servicenow")
    # The middle identity keeps the rule that actually merged it.
    assert next(c for c in final.constituents if c.entity_id == "c").rule == (
        em.RULE_ALIAS_MAPPING
    )


def test_re_folding_the_same_constituent_changes_nothing():
    """The property that makes applying a merge twice safe."""
    first = em.build_merged_provenance(
        _row("e1", name="A", source="servicenow"),
        _row("e2", name="A", source="jira"),
        rule=em.RULE_EXPLICIT_REFERENCE,
        merged_at="2026-01-02T00:00:00+00:00",
    )
    survivor = _row(
        "e1", name="A", source="servicenow",
        metadata={em.METADATA_MERGE_PROVENANCE: first.to_dict()},
    )
    again = em.build_merged_provenance(
        survivor, _row("e2", name="A", source="jira"),
        rule=em.RULE_EXPLICIT_REFERENCE, merged_at="2026-09-09T00:00:00+00:00",
    )
    assert [c.to_dict() for c in again.constituents] == [
        c.to_dict() for c in first.constituents
    ]


def test_the_constituent_order_is_deterministic():
    kwargs = dict(rule=em.RULE_EXPLICIT_REFERENCE, merged_at="2026-01-02T00:00:00+00:00")
    a = em.build_merged_provenance(
        _row("e1", name="A", source="servicenow"), _row("e2", name="A", source="jira"), **kwargs
    )
    b = em.build_merged_provenance(
        _row("e1", name="A", source="servicenow"), _row("e2", name="A", source="jira"), **kwargs
    )
    assert a.to_dict() == b.to_dict()
    assert a.constituents[0].is_origin is True, "the origin leads the list"


# ── survivor selection ──────────────────────────────────────────────────────


def test_an_existing_survivor_wins_so_merges_do_not_fragment():
    merged = em.build_merged_provenance(
        _row("e2", name="A", source="jira"), _row("e9", name="A", source="git"),
        rule=em.RULE_ALIAS_MAPPING,
    )
    already = _row(
        "e2", name="A", source="jira", created_at="2026-05-05T00:00:00+00:00",
        metadata={em.METADATA_MERGE_PROVENANCE: merged.to_dict()},
    )
    older_plain = _row("e1", name="A", source="servicenow", created_at="2026-01-01T00:00:00+00:00")

    assert em.choose_survivor([older_plain, already])["id"] == "e2"


def test_a_stable_record_id_beats_a_name_derived_row():
    with_id = _row("e2", name="A", source="jira", record_id="PAY",
                   created_at="2026-05-05T00:00:00+00:00")
    without_id = _row("e1", name="A", source="slack", record_id=None,
                      created_at="2026-01-01T00:00:00+00:00")
    assert em.choose_survivor([without_id, with_id])["id"] == "e2"


def test_the_oldest_row_wins_when_both_are_equally_identified():
    older = _row("e2", name="A", source="jira", created_at="2026-01-01T00:00:00+00:00")
    newer = _row("e1", name="A", source="git", created_at="2026-05-05T00:00:00+00:00")
    assert em.choose_survivor([newer, older])["id"] == "e2"


def test_the_id_breaks_a_total_tie_so_a_rerun_never_flips():
    same = dict(name="A", source="servicenow", created_at="2026-01-01T00:00:00+00:00")
    a, b = _row("e1", **same), _row("e2", **same)
    assert em.choose_survivor([a, b])["id"] == em.choose_survivor([b, a])["id"] == "e1"


def test_choosing_from_nothing_is_none_not_a_crash():
    assert em.choose_survivor([]) is None
    assert em.choose_survivor([{"id": ""}]) is None


# ── reading provenance back ─────────────────────────────────────────────────


def test_provenance_round_trips_through_metadata():
    built = em.build_merged_provenance(
        _row("e1", name="A", source="servicenow", record_id="sn-1"),
        _row("e2", name="A", source="jira", record_id="PAY"),
        rule=em.RULE_EXPLICIT_REFERENCE,
    )
    metadata = {em.METADATA_MERGE_PROVENANCE: built.to_dict()}
    read = em.MergeProvenance.from_metadata("e1", metadata)
    assert read.to_dict() == built.to_dict()
    assert read.is_merged is True


def test_an_unmerged_entity_is_not_reported_as_merged():
    assert em.MergeProvenance.from_metadata("e1", {}).is_merged is False
    assert em.MergeProvenance.from_metadata("e1", None).constituents == ()
    solo = em.MergeProvenance(
        entity_id="e1",
        constituents=(em.identity_of(_row("e1", name="A", source="servicenow"), is_origin=True),),
    )
    assert solo.is_merged is False, "one identity is not a merge"


def test_corrupt_provenance_degrades_instead_of_breaking_a_read():
    for bad in ("not-json", {"merge_provenance": "nonsense"},
                {"merge_provenance": {"constituents": "nope"}},
                {"merge_provenance": {"constituents": [{"no_id": 1}]}}):
        provenance = em.MergeProvenance.from_metadata("e1", em._loads(bad) if isinstance(bad, str) else bad)
        assert provenance.constituents == ()


# ── what may merge ──────────────────────────────────────────────────────────


def test_only_the_auto_merge_tiers_map_to_a_rule():
    """The propose-only tier has no rule, so it cannot merge even here — the
    second, independent gate behind T1's AUTO_MERGE_TIERS."""
    from app import cross_source_resolution as csr

    assert em._RULE_FOR_TIER.keys() == set(csr.AUTO_MERGE_TIERS)
    assert csr.TIER_NAME_SIMILARITY not in em._RULE_FOR_TIER


def test_a_non_merge_decision_applies_nothing(monkeypatch):
    """A proposed / ambiguous / unresolved decision carries no authority."""
    from app import cross_source_resolution as csr

    def _explode(*_a, **_k):
        raise AssertionError("apply_merge must not be called for a non-merge decision")

    monkeypatch.setattr(em, "apply_merge", _explode)

    subject = csr.ResolutionEntity(
        entity_id="e1", org_id="org_a", entity_type="system", display_name="A",
        canonical_name="a", source_system="servicenow", source_record_id="sn-1",
    )
    for status in (csr.STATUS_PROPOSED, csr.STATUS_AMBIGUOUS, csr.STATUS_UNRESOLVED):
        decision = csr.ResolutionDecision(subject=subject, status=status)
        report = em.apply_resolution_decisions("org_a", [decision])
        assert report.merged == 0
        assert report.outcomes == ()


def test_a_merge_decision_from_an_unmergeable_tier_is_skipped_loudly(monkeypatch):
    """Belt and braces: even a decision that CLAIMS merge authority is refused
    when its tier has no rule."""
    from app import cross_source_resolution as csr

    monkeypatch.setattr(
        em, "apply_merge",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not merge")),
    )
    subject = csr.ResolutionEntity(
        entity_id="e1", org_id="org_a", entity_type="system", display_name="A",
        canonical_name="a", source_system="servicenow",
    )
    target = csr.ResolutionEntity(
        entity_id="e2", org_id="org_a", entity_type="system", display_name="A",
        canonical_name="a", source_system="jira",
    )
    decision = csr.ResolutionDecision(
        subject=subject, status=csr.STATUS_RESOLVED,
        tier=csr.TIER_NAME_SIMILARITY, merge_target=target,
    )
    report = em.apply_resolution_decisions("org_a", [decision])
    assert report.merged == 0
    assert report.skipped == 1
    assert "not permitted to merge" in report.outcomes[0].reason


def test_the_contract_fixture_shape_really_produces_a_proposal():
    """Guard for the contract suite's seeded estate, run WITHOUT a database.

    ``tests/contract/test_entity_merge_contract.py`` seeds three entities and
    asserts that the name-only pair is proposed (and never auto-merged). Tier 3
    needs more than a shared name — it needs a corroborating observed
    relationship — so an estate missing the second edge silently produces no
    proposal and those tests fail far from the cause. This mirrors the fixture in
    memory so the shape is pinned where it is cheap to check.
    """
    from app import cross_source_resolution as csr

    def _entity(eid, name, source, record_id):
        return csr.ResolutionEntity(
            entity_id=eid, org_id="org_a", entity_type="system", display_name=name,
            canonical_name=" ".join(name.split()).lower(),
            source_system=source, source_record_id=record_id,
            cross_references=csr.extract_cross_references(
                {"cross_references": [{"system": "jira", "record_id": "PAY"}]},
                own_system=source,
            ) if eid == "sn" else (),
        )

    sn = _entity("sn", "Payments Platform", "servicenow", "sn-1")
    jira = _entity("jira", "Payments", "jira", "PAY")
    git = _entity("git", "payments", "git", "repo-1")
    pool = [sn, jira, git]
    rels = csr.build_relationship_index([
        {"from_entity_id": "jira", "to_entity_id": "team",
         "relationship_type": "owns", "inferred": False},
        {"from_entity_id": "git", "to_entity_id": "team",
         "relationship_type": "owns", "inferred": False},
    ])

    decisions = csr.resolve_entities(pool, pool, relationship_index=rels)
    by_subject = {d.subject.entity_id: d for d in decisions}

    # The explicit cross-reference auto-merges...
    assert by_subject["sn"].is_merge is True
    assert by_subject["sn"].tier == csr.TIER_EXPLICIT_REFERENCE
    # ...and the name-only pair is PROPOSED, never merged.
    assert by_subject["git"].status == csr.STATUS_PROPOSED
    assert by_subject["git"].is_merge is False
    assert by_subject["git"].proposals[0].target.entity_id == "jira"

    # And the corroboration really is load-bearing: drop the shared neighbour and
    # the proposal correctly disappears — which is exactly how the contract
    # fixture failed in CI.
    without = csr.resolve_entities(
        pool, pool,
        relationship_index=csr.build_relationship_index([
            {"from_entity_id": "jira", "to_entity_id": "team",
             "relationship_type": "owns", "inferred": False},
        ]),
    )
    assert {d.subject.entity_id: d for d in without}["git"].status == (
        csr.STATUS_UNRESOLVED
    )


def test_merging_an_entity_with_itself_is_refused_not_written():
    assert em.apply_merge("org_a", "e1", "e1", rule=em.RULE_ALIAS_MAPPING).outcome == (
        em.OUTCOME_SKIPPED
    )


@pytest.mark.parametrize("org,left,right", [("", "a", "b"), ("org_a", "", "b"), ("org_a", "a", "")])
def test_an_unusable_merge_request_raises(org, left, right):
    with pytest.raises(em.EntityMergeError):
        em.apply_merge(org, left, right, rule=em.RULE_ALIAS_MAPPING)


# ── audit ───────────────────────────────────────────────────────────────────


def test_the_audit_event_type_is_registered():
    from app.middleware.audit import AUDIT_EVENT_REGISTRY, ENTITY_MERGED

    assert ENTITY_MERGED in AUDIT_EVENT_REGISTRY


def test_a_merge_is_audited_with_the_pair_the_rule_and_the_actor(monkeypatch):
    events = []
    monkeypatch.setattr("app.middleware.audit.log_event", lambda et, **kw: events.append((et, kw)))

    provenance = em.build_merged_provenance(
        _row("e1", name="A", source="servicenow"),
        _row("e2", name="A", source="jira"),
        rule=em.RULE_CONFIRMED_PROPOSAL,
    )
    em._audit_merge(
        "org_a",
        em.MergeOutcome(
            outcome=em.OUTCOME_MERGED, rule=em.RULE_CONFIRMED_PROPOSAL,
            survivor_id="e1", merged_entity_id="e2",
        ),
        actor="analyst-1",
        provenance=provenance,
    )

    assert len(events) == 1
    event_type, payload = events[0]
    assert event_type == "entity_merged"
    assert payload["user_id"] == "analyst-1"
    assert payload["survivor_entity_id"] == "e1"
    assert payload["merged_entity_id"] == "e2"
    assert payload["rule"] == em.RULE_CONFIRMED_PROPOSAL
    assert payload["constituent_count"] == 2
    assert payload["source_systems"] == ["jira", "servicenow"]


def test_an_audit_failure_never_fails_the_merge(monkeypatch):
    """The merge is already committed; a broken audit store must not raise into
    the caller."""
    def _boom(*_a, **_k):
        raise RuntimeError("audit store down")

    monkeypatch.setattr("app.middleware.audit.log_event", _boom)
    em._audit_merge(
        "org_a",
        em.MergeOutcome(outcome=em.OUTCOME_MERGED, rule=em.RULE_ALIAS_MAPPING, survivor_id="e1"),
        actor="system",
        provenance=em.MergeProvenance(entity_id="e1"),
    )  # must not raise


# ── the merge never destroys ────────────────────────────────────────────────


def test_the_applier_never_deletes_an_entity_or_an_edge():
    """Deleting a constituent would destroy the very evidence AC2 requires and
    make T4's unmerge impossible. A grep-level guard, because the damage is
    silent."""
    import inspect

    source = inspect.getsource(em)
    for forbidden in (
        "DELETE FROM entities",
        "DELETE FROM entity_relationships",
        "DROP TABLE",
    ):
        assert forbidden not in source, f"the merge applier must never {forbidden!r}"


def test_the_applier_never_rewrites_resolution_status():
    """resolution_status records how the STANDING engine resolved that row —
    a different fact from "this was merged", and overwriting it destroys it."""
    import inspect

    source = inspect.getsource(em)
    assert "resolution_status =" not in source
    assert "SET metadata = %s, updated_at = %s" in source
