"""2.0-B4 T1 — the normalised concept set, its contracts, and conformance (AC1).

AC1 has three clauses, and each is only worth something if a specific way of faking
it is closed off:

  * **documented** — a doc that drifts from the model is worse than none, so the
    contracts are DATA and a test asserts every field they name exists on the class
    that implements the concept. A contract cannot describe a model that is not
    there, and a model field cannot appear with no contract entry.
  * **versioned** — a version nobody bumps is decoration. The bump rules are stated
    in the module and pinned here, and the contract/set versions are separated
    because adding a concept and adding a required field break different things.
  * **each connector declares its conformance** — a declaration is worthless if it
    can claim more than it delivers, so `supported` is refused without a named
    mapper, a gap is refused without a reason, and a partial declaration is refused
    outright.

DB-free: the concept set is pure definition.
"""
from __future__ import annotations

import dataclasses

import pytest

from app.provenance import EvidencePointer, utc_now_iso
from discovery import concepts as C
from discovery.concepts import conformance as conf
from discovery.concepts import contracts as ct
from discovery.concepts import model as m
from discovery.signals.operational_event import CommonSignal, OperationalEvent


def _ptr(source_system: str = "servicenow") -> dict:
    return EvidencePointer(
        source_system=source_system,
        source_artifact="REC-1",
        source_timestamp=utc_now_iso(),
        origin="observed",
    ).to_dict()


def _spine(source_system: str = "servicenow") -> dict:
    return {
        "org_id": "acme",
        "source_system": source_system,
        "signal_id": "REC-1",
        "observed_at": utc_now_iso(),
        "provenance": _ptr(source_system),
    }


# ── the set is what the story names ─────────────────────────────────────────


def test_the_concept_set_is_exactly_the_seven_the_story_names():
    assert C.CONCEPT_SET == {
        "work_item", "actor_group", "artifact", "state_transition",
        "approval", "assignment", "entity_reference",
    }


def test_every_concept_has_a_class_and_a_contract():
    """A concept named but not implemented, or implemented but not contracted, would
    be a hole a pack author only finds at runtime."""
    for concept in C.CONCEPT_SET:
        assert concept in m.CONCEPT_CLASSES, f"{concept} has no class"
        assert concept in ct.CONTRACTS, f"{concept} has no contract"
    assert set(m.CONCEPT_CLASSES) == C.CONCEPT_SET
    assert set(ct.CONTRACTS) == C.CONCEPT_SET


def test_the_six_observations_are_profiles_of_the_common_signal_model():
    """The widening claim: these specialise MSP-B0's spine rather than paralleling
    it, so provenance and tenancy are inherited, not reinvented."""
    for concept in C.CONCEPT_SET - {C.CONCEPT_ENTITY_REFERENCE}:
        cls = m.CONCEPT_CLASSES[concept]
        assert issubclass(cls, CommonSignal), f"{concept} is not a CommonSignal profile"
        assert issubclass(cls, m.ConceptSignal)


def test_the_operational_event_remains_a_sibling_profile_not_a_parent():
    """B0's profile is unchanged by the widening — the concepts sit beside it on the
    same spine. If OperationalEvent had become a base, every cloud connector would
    have inherited workflow fields that mean nothing to it."""
    assert issubclass(OperationalEvent, CommonSignal)
    assert not issubclass(m.WorkItem, OperationalEvent)
    assert not issubclass(OperationalEvent, m.ConceptSignal)


def test_entity_reference_is_a_value_type_not_an_observation():
    """It has no provenance of its own because it is always carried by an observation
    that does. Modelling it as a signal would force every construction to invent a
    spine, and an invented spine is what R16-B1 forbids. ResourceRef plays the same
    role in B0."""
    assert not issubclass(m.EntityReference, CommonSignal)
    ref = m.EntityReference(
        entity_type="team", source_system="servicenow", source_record_id="grp-1"
    )
    assert ref.is_resolved is False, "unresolved is the honest default"


# ── closed vocabularies fail at construction (B0's rule) ────────────────────


@pytest.mark.parametrize("field,bad", [
    ("work_item_type", "ticket"),
    ("status_category", "Work in Progress"),   # a native status, not a category
    ("priority", "P1"),                        # a native priority
])
def test_a_native_value_is_refused_where_a_normalised_token_is_required(field, bad):
    """The whole point of the vocabulary: an unmapped native value fails at the
    connector instead of reaching a detector as an unrecognised string."""
    with pytest.raises(ValueError, match="must be one of"):
        m.WorkItem(**_spine(), **{field: bad})


def test_the_refusal_tells_the_implementer_what_to_do():
    """An error that only says "invalid" sends someone to read source. This one names
    the alternative: map it, or declare the gap."""
    with pytest.raises(ValueError, match="declare the gap"):
        m.WorkItem(**_spine(), work_item_type="ticket")


@pytest.mark.parametrize("cls,field,bad", [
    (m.ActorGroup, "group_type", "individual"),
    (m.Artifact, "artifact_type", "spreadsheet"),
    (m.Artifact, "content_type", "markdown"),
    (m.StateTransition, "transition_type", "moved"),
    (m.Approval, "decision", "maybe"),
    (m.Approval, "approval_type", "informal"),
    (m.Assignment, "assignment_type", "handoff"),
])
def test_every_profile_validates_its_vocabularies(cls, field, bad):
    kwargs = dict(_spine())
    if cls is m.ActorGroup:
        kwargs["name"] = "Level 2 Support"
    with pytest.raises(ValueError, match="must be one of"):
        cls(**kwargs, **{field: bad})


def test_a_valid_work_item_keeps_the_native_status_for_trace_back():
    """Normalising must not destroy the source's own value — a trace has to reach the
    record as the source described it."""
    item = m.WorkItem(
        **_spine(), work_item_type="incident", status_category="in_progress",
        native_status="Work in Progress", priority="high", reference="INC0000001",
    )
    assert item.status_category == "in_progress"
    assert item.native_status == "Work in Progress"
    assert item.to_dict()["native_status"] == "Work in Progress"


def test_cancelled_is_not_open_and_is_not_resolved():
    """Treating abandoned work as completed would overstate throughput for every
    detector downstream — the distinction is why 'cancelled' is its own category."""
    cancelled = m.WorkItem(**_spine(), status_category="cancelled")
    assert cancelled.is_open is False
    assert cancelled.status_category != "resolved"


def test_a_pending_approval_is_representable():
    """An undecided approval is exactly what an approval-bottleneck detector measures,
    so it must be a value rather than an absence."""
    approval = m.Approval(**_spine(), decision="pending")
    assert approval.is_decided is False
    assert "pending" in m.APPROVAL_DECISIONS


# ── groups, never individuals ───────────────────────────────────────────────


def test_actor_group_cannot_denote_an_individual():
    """The platform's standing rule is that output names groups, queues and processes
    only. ActorGroup is the ONLY actor concept precisely so that rule has nowhere to
    leak — a concept set offering a bare "actor" would make violating it the path of
    least resistance for every future pack author."""
    assert "person" not in m.ACTOR_GROUP_TYPES
    assert "individual" not in m.ACTOR_GROUP_TYPES
    assert "user" not in m.ACTOR_GROUP_TYPES
    # No concept in the set is named for an individual actor.
    assert not any("actor" == c or "person" in c or "user" in c for c in C.CONCEPT_SET)


def test_actor_group_exposes_an_aggregate_count_and_no_roster():
    group = m.ActorGroup(**_spine(), group_type="queue", name="L2 Queue", member_count=7)
    payload = group.to_dict()
    assert payload["member_count"] == 7
    assert not any("member" in k and k != "member_count" for k in payload), (
        "an aggregate is permitted; a roster is not part of the contract at any version"
    )


def test_group_bearing_fields_are_references_not_names():
    """A string field would invite a mapper to put a person's name in it."""
    for cls, field_name in (
        (m.WorkItem, "assigned_group"),
        (m.Approval, "approver_group"),
        (m.Assignment, "assigned_to"),
        (m.StateTransition, "actor_group"),
    ):
        hints = {f.name: f.type for f in dataclasses.fields(cls)}
        assert "EntityReference" in str(hints[field_name]), (
            f"{cls.__name__}.{field_name} must be an EntityReference"
        )

    with pytest.raises(ValueError, match="EntityReference"):
        m.WorkItem(**_spine(), assigned_group="Alice Smith")


# ── the contracts are documented AND cannot drift from the model ────────────


def test_every_contract_field_exists_on_the_class_that_implements_it():
    """The anti-drift check. A contract that named a field the model lacks would send
    a connector author to populate something that does not exist."""
    for concept, contract in ct.CONTRACTS.items():
        cls = m.CONCEPT_CLASSES[concept]
        actual = {f.name for f in dataclasses.fields(cls)}
        for field in contract.fields:
            assert field.name in actual, (
                f"{concept} contract names {field.name!r}, absent from {cls.__name__}"
            )


def test_every_model_field_is_covered_by_its_contract():
    """The other direction: a field that appears with no contract entry is undocumented
    surface a pack author would have to discover by reading source."""
    for concept, contract in ct.CONTRACTS.items():
        cls = m.CONCEPT_CLASSES[concept]
        contracted = {f.name for f in contract.fields}
        for field in dataclasses.fields(cls):
            if field.name == "concept":
                continue  # set by the profile itself, not by a mapper
            assert field.name in contracted, (
                f"{cls.__name__}.{field.name} has no contract entry"
            )


def test_every_vocabulary_a_contract_names_actually_exists():
    """A contract referencing a vocabulary by a name that does not resolve would be
    unenforceable — and would look enforced."""
    for contract in ct.CONTRACTS.values():
        for field in contract.fields:
            if field.vocabulary:
                assert isinstance(ct.vocabulary(field.vocabulary), frozenset)


def test_the_spine_is_documented_once_and_shared_by_every_observation():
    """Seven copies of the spine contract would drift; one cannot."""
    spine_names = {f.name for f in ct.SPINE_FIELDS}
    for concept in C.CONCEPT_SET - {C.CONCEPT_ENTITY_REFERENCE}:
        contracted = {f.name for f in ct.CONTRACTS[concept].fields}
        assert spine_names <= contracted, f"{concept} is missing spine fields"
    # The value type carries no observation spine, and must not pretend to.
    ref_fields = {f.name for f in ct.ENTITY_REFERENCE_CONTRACT.fields}
    assert "provenance" not in ref_fields
    assert "observed_at" not in ref_fields


def test_provenance_is_required_on_every_observation():
    """R16-B1: a concept with no traceable origin is not persistable."""
    for concept in C.CONCEPT_SET - {C.CONCEPT_ENTITY_REFERENCE}:
        required = ct.CONTRACTS[concept].required_fields
        assert "provenance" in required
        assert "org_id" in required, "every signal is org-scoped (tenancy)"

    with pytest.raises(ValueError, match="provenance"):
        m.WorkItem(org_id="acme", source_system="servicenow", signal_id="x",
                   observed_at=utc_now_iso(), provenance={})


def test_every_contract_carries_rules_a_field_constraint_cannot_express():
    """The rules that get violated precisely because they are not mechanical —
    'cancelled is not resolved', 'reopen is not a status_change', 'never synthesise a
    group from a person's name'."""
    for concept, contract in ct.CONTRACTS.items():
        assert contract.rules, f"{concept} states no non-mechanical rules"
        assert contract.purpose.strip()


# ── versioning ──────────────────────────────────────────────────────────────


def test_the_set_and_each_contract_are_versioned():
    assert ct.CONCEPT_SET_VERSION >= 1
    for concept, contract in ct.CONTRACTS.items():
        assert contract.version >= 1, f"{concept} is unversioned"
        assert contract.to_dict()["concept_set_version"] == ct.CONCEPT_SET_VERSION


def test_the_two_version_levels_are_separate_because_they_break_different_things():
    """Adding a concept does not invalidate any existing declaration; adding a
    required field to an existing concept invalidates every declaration for it. One
    number could not express both without overstating or understating the breakage."""
    rules = " ".join(ct.BREAKING_CHANGE_RULES).lower()
    assert "adding a concept" in rules and "bumps no contract version" in rules
    assert "required field" in rules and "bumps that contract" in rules
    assert "optional field does not bump" in rules


def test_the_bump_rules_are_stated_in_the_module_not_left_to_memory():
    """A rule kept only in a reviewer's head is a rule that gets forgotten at the
    point it costs a connector author their conformance."""
    assert len(ct.BREAKING_CHANGE_RULES) >= 5
    for rule in ct.BREAKING_CHANGE_RULES:
        assert rule.strip().endswith(".") or "bump" in rule.lower()


def test_the_contract_summary_is_the_serialisable_documentation_surface():
    """What a pack author reads and a reviewer diffs to see whether a bump was owed."""
    import json

    summary = ct.contract_summary()
    assert summary["concept_set_version"] == ct.CONCEPT_SET_VERSION
    assert sorted(summary["concepts"]) == sorted(C.CONCEPT_SET)
    assert set(summary["contract_versions"]) == C.CONCEPT_SET
    assert json.loads(json.dumps(summary)) == summary


def test_an_unknown_concept_is_refused_with_the_valid_set_named():
    with pytest.raises(KeyError, match="not a normalised concept"):
        ct.get_contract("sandwich")


# ── conformance: a declaration that cannot claim more than it delivers ──────


def test_every_shipped_connector_that_declares_covers_the_whole_concept_set():
    """A partial declaration would leave the omitted concept silently unmapped —
    the exact ambiguity the registry exists to remove."""
    for connector_id, decl in conf.CONFORMANCE.items():
        positions = {c.concept for c in decl.concepts}
        assert positions == C.CONCEPT_SET, f"{connector_id} does not cover the set"


def test_a_partial_declaration_is_refused():
    with pytest.raises(conf.ConformanceError, match="omits"):
        conf.ConnectorConformance(
            connector_id="halfhearted",
            source_family="test",
            concepts=(conf.ConceptConformance(C.CONCEPT_WORK_ITEM, conf.STATUS_DECLARED),),
        )


def test_supported_cannot_be_claimed_without_naming_a_mapper():
    """The strongest claim must point at readable code, so it cannot be made by
    editing a comment."""
    with pytest.raises(conf.ConformanceError, match="must name the mapper"):
        conf.ConceptConformance(C.CONCEPT_WORK_ITEM, conf.STATUS_SUPPORTED)


def test_a_gap_without_a_reason_is_refused():
    """An unexplained gap is indistinguishable from an oversight."""
    with pytest.raises(conf.ConformanceError, match="requires a reason"):
        conf.ConceptConformance(C.CONCEPT_APPROVAL, conf.STATUS_GAP)
    with pytest.raises(conf.ConformanceError, match="requires a reason"):
        conf.ConceptConformance(C.CONCEPT_APPROVAL, conf.STATUS_NOT_APPLICABLE)


def test_only_supported_counts_as_conformance():
    """`declared` is intent. If it counted, a portable detector would run against a
    connector with no mapper and silently find nothing."""
    declared = conf.ConceptConformance(C.CONCEPT_WORK_ITEM, conf.STATUS_DECLARED)
    assert declared.conforms is False
    supported = conf.ConceptConformance(
        C.CONCEPT_WORK_ITEM, conf.STATUS_SUPPORTED, mapper="map_servicenow_work_item"
    )
    assert supported.conforms is True


def test_nothing_claims_supported_yet_which_is_the_honest_state_at_t1():
    """T1 defines the set, the contracts and this mechanism; the mappers are T2/T3.
    A `supported` here would be a claim with no code behind it."""
    for connector_id, decl in conf.CONFORMANCE.items():
        assert decl.supported == (), (
            f"{connector_id} claims support before a mapper exists"
        )
    assert all(not v for v in conf.conformance_summary()["supported_by_concept"].values())


def test_gap_and_not_applicable_are_reported_differently():
    """A cloud-event stream having no approvals is not a shortcoming to be fixed,
    whereas an ITSM tool whose approvals we cannot read is. A backlog conflating them
    fills with items nobody intends to do, and then gets ignored wholesale."""
    gaps = conf.declared_gaps()
    for connector_id, entries in gaps.items():
        for entry in entries:
            assert entry.status == conf.STATUS_GAP, (
                f"{connector_id} reports a non-gap in declared_gaps()"
            )
    # aws_events is entirely not-applicable for the workflow concepts, so it must NOT
    # appear as a gap.
    assert "aws_events" not in gaps
    # jira genuinely cannot read approvals, so it must.
    assert "jira" in gaps
    assert any(g.concept == C.CONCEPT_APPROVAL for g in gaps["jira"])


def test_every_declared_connector_ships_its_ingestion():
    """R191-R1's anchoring rule: a connector whose ingestion does not ship cannot
    conform, because there is nothing to conform."""
    from app.connector_roadmap import SHIPPED_CONNECTOR_IDS

    for connector_id in conf.CONFORMANCE:
        assert connector_id in SHIPPED_CONNECTOR_IDS, (
            f"{connector_id} declares conformance but its ingestion does not ship"
        )


def test_declarations_are_pinned_to_the_concept_set_version_they_were_written_against():
    """A declaration written before a concept existed cannot have a position on it, so
    trusting it silently would report conformance nobody assessed."""
    assert conf.stale_declarations() == ()
    stale = conf.ConnectorConformance(
        connector_id="ancient",
        source_family="test",
        concepts=tuple(
            conf.ConceptConformance(c, conf.STATUS_NOT_APPLICABLE, reason="test")
            for c in sorted(C.CONCEPT_SET)
        ),
        concept_set_version=0,
    )
    assert stale.concept_set_version != ct.CONCEPT_SET_VERSION


def test_connectors_supporting_reads_code_not_intent():
    for concept in C.CONCEPT_SET:
        assert conf.connectors_supporting(concept) == (), "no mappers exist at T1"
    with pytest.raises(KeyError, match="not a normalised concept"):
        conf.connectors_supporting("sandwich")


def test_the_conformance_summary_is_serialisable_and_counts_gaps():
    import json

    summary = conf.conformance_summary()
    assert summary["gap_count"] == sum(
        len(g) for g in conf.declared_gaps().values()
    )
    assert summary["gap_count"] > 0, "real gaps exist and are recorded, not hidden"
    assert json.loads(json.dumps(summary)) == summary


def test_an_unknown_connector_is_refused_with_the_declared_set_named():
    with pytest.raises(KeyError, match="no conformance declaration"):
        conf.get_conformance("nonexistent")


# ── shared vocabularies are shared, not copied ──────────────────────────────


def test_entity_reference_types_match_the_knowledge_graph_exactly():
    """A concept reference and a graph entity must not disagree about what kinds of
    thing exist."""
    from database.models.entities import ENTITY_TYPES

    assert m.ENTITY_REFERENCE_TYPES == ENTITY_TYPES


def test_artifact_content_types_cover_the_retrieval_substrate_vocabulary():
    """An artifact classified here needs no re-classification to be chunked, so the
    vocabulary must be the one the substrate already chunks by.

    Anchored on ``retrieval.chunking``'s splitter keys because that is where the
    vocabulary originates. ``structured`` is additional and deliberate: the substrate
    only chunks text, but a concept may describe a record, and a record is neither
    prose nor code nor conversation.
    """
    from app.retrieval.chunking import _SPLITTERS

    substrate = set(_SPLITTERS)
    assert substrate <= m.CONTENT_TYPES, (
        f"the substrate chunks {sorted(substrate - m.CONTENT_TYPES)} which this "
        f"concept set cannot express"
    )
    assert m.CONTENT_TYPES - substrate == {"structured"}


def test_artifact_content_types_align_with_the_assembly_source_types_when_present():
    """2.0-B3 T1 gives the assembler a source-type precedence keyed on the same
    vocabulary. That work is on its own branch, so this check strengthens
    automatically once it merges rather than being dropped now and forgotten.
    """
    try:
        from app.assembly_policy_config import (
            SOURCE_TYPE_CODE,
            SOURCE_TYPE_CONVERSATION,
            SOURCE_TYPE_PROSE,
            SOURCE_TYPE_STRUCTURED,
        )
    except ModuleNotFoundError:
        pytest.skip("2.0-B3 T1 assembly policy not on this branch yet")

    assert m.CONTENT_TYPES == {
        SOURCE_TYPE_PROSE, SOURCE_TYPE_CODE,
        SOURCE_TYPE_CONVERSATION, SOURCE_TYPE_STRUCTURED,
    }


# ── the documentation cannot drift from the code ────────────────────────────


def _doc_text() -> str:
    from pathlib import Path

    # backend/tests/unit/<this file> -> repo root is three levels up.
    doc = Path(__file__).resolve().parents[3] / "docs" / "normalised_concepts.md"
    assert doc.exists(), (
        f"documentation not found at {doc} — AC1 requires the concept set to be "
        f"documented, so a missing doc is a failure, not a skip"
    )
    return doc.read_text(encoding="utf-8")


def test_every_documented_vocabulary_matches_the_code():
    """AC1's 'documented' clause is only worth something if the document is TRUE.

    A vocabulary table is exactly the kind of thing that goes stale the first time
    someone adds a value — and a stale table is worse than no table, because a
    connector author will map against it and fail validation with no idea why.
    """
    import re

    doc = _doc_text()
    for name in (
        "WORK_ITEM_TYPES", "STATUS_CATEGORIES", "PRIORITY_LEVELS", "ACTOR_GROUP_TYPES",
        "ARTIFACT_TYPES", "CONTENT_TYPES", "TRANSITION_TYPES", "APPROVAL_DECISIONS",
        "APPROVAL_TYPES", "ASSIGNMENT_TYPES", "ENTITY_REFERENCE_TYPES",
    ):
        row = re.search(rf"^\| `{name}` \| (.+?) \|$", doc, re.M)
        assert row, f"{name} is not documented in docs/normalised_concepts.md"
        documented = {v.strip() for v in row.group(1).split(",")}
        actual = set(getattr(m, name))
        assert documented == actual, (
            f"{name} has drifted — doc-only={sorted(documented - actual)}, "
            f"code-only={sorted(actual - documented)}"
        )


def test_every_concept_appears_in_the_documentation():
    doc = _doc_text()
    for concept in C.CONCEPT_SET:
        assert f"`{concept}`" in doc, f"{concept} is undocumented"


def test_the_documented_version_matches_the_code():
    """A doc claiming version 1 while the code is on 2 would send a reader to the
    wrong contract."""
    doc = _doc_text()
    assert f"Concept set version: {ct.CONCEPT_SET_VERSION}" in doc
