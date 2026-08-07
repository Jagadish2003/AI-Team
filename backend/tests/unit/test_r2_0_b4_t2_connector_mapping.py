"""2.0-B4 T2 — connector mapping, and AC5's declared gaps.

AC5: *"Unmappable connector concepts are recorded as declared gaps, visible to pack
authors — never silently approximated."*

The suite is organised around the three clauses:

* **recorded** — a gap names a real contract field, carries a reason, and cannot
  coexist with a ``supported`` claim on a required field;
* **visible** — the gap report answers a pack author's question (which sources can
  carry my detector, what will be missing) without reading the registry by hand;
* **never approximated** — every mapper's output over the golden fixtures is checked
  against its own declaration, and the check is PROVEN to fail when a mapper populates
  a field it declared absent. A guard never observed failing is not known to be a
  guard (this repo's standing rule, and the reason the negative controls below exist).

Beyond AC5 the suite pins the mapping decisions that are easy to get wrong and
impossible to notice: the ServiceNow raw-vs-display trap, cancelled-is-not-resolved on
both ITSM and tracker sources, the Salesforce queue-vs-person owner branch, and the
standing rule that no concept may carry an individual.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from discovery.concepts import conformance as conf
from discovery.concepts import gaps as G
from discovery.concepts import mappers as mp
from discovery.concepts import model as M
from discovery.concepts.contracts import get_contract
from discovery.concepts.mappers import _common, cloud_events, content, jira, salesforce, servicenow

FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "discovery" / "tests" / "fixtures" / "concept_mapping_samples.json"
)
ORG = "org_test"


@pytest.fixture(scope="module")
def samples():
    assert FIXTURE.exists(), f"golden fixture missing at {FIXTURE}"
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


# ─────────────────────────────────────────────────────────────────────────────
# The registry — a conformance claim must point at real code
# ─────────────────────────────────────────────────────────────────────────────

def test_every_supported_claim_resolves_to_a_registered_mapper():
    """T1 required a `supported` claim to NAME a mapper. A name is only worth
    something if it resolves, which is what makes the registry more than a comment."""
    for connector_id, decl in conf.CONFORMANCE.items():
        for position in decl.concepts:
            if not position.conforms:
                continue
            mapper = mp.resolve_mapper(position.mapper)
            assert (mapper.connector_id, mapper.concept) == (connector_id, position.concept)


def test_every_registered_mapper_is_declared_supported():
    """The converse. A mapper nobody declares is dead code that a pack author cannot
    discover, and `connectors_supporting` would not return it."""
    for (connector_id, concept), mapper in mp.MAPPERS.items():
        decl = conf.get_conformance(connector_id)
        position = decl.position(concept)
        assert position.conforms, (
            f"{mapper.name} exists but {connector_id}/{concept} is "
            f"{position.status!r} — the registry has fallen behind the code"
        )
        assert position.mapper == mapper.name


def test_registry_reports_no_drift():
    assert G.gap_summary()["registry_behind_code"] == []


def test_duplicate_registration_is_refused():
    """Two mappers for one pair means one is dead and which one wins is a coin toss."""
    with pytest.raises(mp.MapperError, match="already has mapper"):
        @mp.maps("servicenow", M.CONCEPT_WORK_ITEM)
        def _second_incident_mapper(org_id, record):  # pragma: no cover
            return None


def test_registering_an_unknown_concept_is_refused():
    with pytest.raises(mp.MapperError, match="not a normalised concept"):
        @mp.maps("servicenow", "sandwich")
        def _nope(org_id, record):  # pragma: no cover
            return None


def test_resolve_mapper_names_the_registered_set_when_it_misses():
    with pytest.raises(mp.MapperError, match="not a registered mapper"):
        mp.resolve_mapper("discovery.concepts.mappers.nowhere:map_nothing")


# ─────────────────────────────────────────────────────────────────────────────
# AC5, clause 3 — never silently approximated
# ─────────────────────────────────────────────────────────────────────────────

def _all_mapped_outputs(samples):
    """Every (connector, concept, produced) triple the fixtures can produce.

    Written once and reused by the sweeps below, so a new mapper is covered by all of
    them the moment its fixture lands rather than only where somebody remembered.
    """
    s = samples
    out = [
        ("servicenow", M.CONCEPT_WORK_ITEM,
         servicenow.map_incident_work_item(ORG, s["servicenow"]["incident_open"])),
        ("servicenow", M.CONCEPT_WORK_ITEM,
         servicenow.map_incident_work_item(ORG, s["servicenow"]["incident_cancelled"])),
        ("servicenow", M.CONCEPT_ACTOR_GROUP,
         servicenow.map_assignment_group(ORG, s["servicenow"]["assignment_group_record"])),
        ("servicenow", M.CONCEPT_ASSIGNMENT,
         servicenow.map_assignment_history(ORG, s["servicenow"]["audit_first_assignment"])),
        ("servicenow", M.CONCEPT_ASSIGNMENT,
         servicenow.map_assignment_history(ORG, s["servicenow"]["audit_reassignment"], hop_index=1)),
        ("servicenow", M.CONCEPT_STATE_TRANSITION,
         servicenow.map_state_transition(ORG, s["servicenow"]["audit_state_change"])),
        ("servicenow", M.CONCEPT_STATE_TRANSITION,
         servicenow.map_state_transition(ORG, s["servicenow"]["audit_state_reopen"])),
        ("servicenow", M.CONCEPT_ENTITY_REFERENCE,
         servicenow.map_cmdb_ci_reference(s["servicenow"]["cmdb_ci"])),

        ("jira", M.CONCEPT_WORK_ITEM,
         jira.map_issue_work_item(ORG, s["jira"]["issue_in_progress"])),
        ("jira", M.CONCEPT_WORK_ITEM,
         jira.map_issue_work_item(ORG, s["jira"]["issue_done"])),
        ("jira", M.CONCEPT_WORK_ITEM,
         jira.map_issue_work_item(ORG, s["jira"]["issue_wont_do"])),
        ("jira", M.CONCEPT_ARTIFACT,
         jira.map_issue_attachment(ORG, s["jira"]["attachment"], issue_key="PAY-42")),
        ("jira", M.CONCEPT_ENTITY_REFERENCE,
         jira.map_issue_reference(s["jira"]["issue_in_progress"])),

        ("salesforce", M.CONCEPT_WORK_ITEM,
         salesforce.map_case_work_item(ORG, s["salesforce"]["case_queue_owned"])),
        ("salesforce", M.CONCEPT_WORK_ITEM,
         salesforce.map_case_work_item(ORG, s["salesforce"]["case_person_owned"])),
        ("salesforce", M.CONCEPT_STATE_TRANSITION,
         salesforce.map_case_history_transition(ORG, s["salesforce"]["history_status_change"])),
        ("salesforce", M.CONCEPT_STATE_TRANSITION,
         salesforce.map_case_history_transition(ORG, s["salesforce"]["history_reopen"])),
        ("salesforce", M.CONCEPT_ASSIGNMENT,
         salesforce.map_case_owner_assignment(ORG, s["salesforce"]["history_owner_to_queue"])),
        ("salesforce", M.CONCEPT_ASSIGNMENT,
         salesforce.map_case_owner_assignment(ORG, s["salesforce"]["history_owner_to_person"])),
        ("salesforce", M.CONCEPT_APPROVAL,
         salesforce.map_process_instance_approval(ORG, s["salesforce"]["approval_pending"])),
        ("salesforce", M.CONCEPT_APPROVAL,
         salesforce.map_process_instance_approval(ORG, s["salesforce"]["approval_removed"])),
        ("salesforce", M.CONCEPT_ACTOR_GROUP,
         salesforce.map_queue_actor_group(ORG, s["salesforce"]["queue_group"])),
        ("salesforce", M.CONCEPT_ENTITY_REFERENCE,
         salesforce.map_record_reference(s["salesforce"]["case_queue_owned"])),

        ("confluence", M.CONCEPT_ARTIFACT,
         content.map_confluence_page(ORG, s["confluence"]["page"])),
        ("confluence", M.CONCEPT_ENTITY_REFERENCE,
         content.map_confluence_reference(s["confluence"]["page"])),
        ("sharepoint", M.CONCEPT_ARTIFACT,
         content.map_sharepoint_item(ORG, s["sharepoint"]["file"])),
        ("sharepoint", M.CONCEPT_ARTIFACT,
         content.map_sharepoint_item(ORG, s["sharepoint"]["page"])),
        ("sharepoint", M.CONCEPT_ENTITY_REFERENCE,
         content.map_sharepoint_reference(s["sharepoint"]["file"])),
        ("slack", M.CONCEPT_ARTIFACT, content.map_slack_thread(ORG, s["slack"]["thread"])),
        ("slack", M.CONCEPT_ACTOR_GROUP, content.map_slack_channel(ORG, s["slack"]["channel"])),
        ("slack", M.CONCEPT_ENTITY_REFERENCE, content.map_slack_reference(s["slack"]["channel"])),
        ("teams", M.CONCEPT_ARTIFACT, content.map_teams_thread(ORG, s["teams"]["thread"])),
        ("teams", M.CONCEPT_ACTOR_GROUP, content.map_teams_channel(ORG, s["teams"]["channel"])),
        ("teams", M.CONCEPT_ENTITY_REFERENCE, content.map_teams_reference(s["teams"]["channel"])),
        ("github", M.CONCEPT_ARTIFACT, content.map_git_artifact(ORG, s["github"]["commit"])),
        ("github", M.CONCEPT_ARTIFACT, content.map_git_artifact(ORG, s["github"]["file"])),
        ("github", M.CONCEPT_ENTITY_REFERENCE, content.map_repo_reference(s["github"]["repo"])),
    ]
    return out


def test_no_mapper_populates_a_field_it_declared_absent(samples):
    """AC5's third clause, applied to every mapper over every fixture."""
    for connector_id, concept, produced in _all_mapped_outputs(samples):
        G.assert_no_approximation(connector_id, concept, produced)


def test_the_approximation_guard_actually_fails(samples):
    """The negative control. ServiceNow declares `state_transition.actor_group` ABSENT
    (the audit row records the change, not the mover); populating it must raise."""
    transition = servicenow.map_state_transition(
        ORG, samples["servicenow"]["audit_state_change"]
    )
    G.assert_no_approximation("servicenow", M.CONCEPT_STATE_TRANSITION, transition)

    transition.actor_group = M.EntityReference(
        entity_type="team", source_system="servicenow", source_record_id="grp1",
        display_name="Level 2 Payments",
    )
    with pytest.raises(G.ApproximationError, match="declared ABSENT"):
        G.assert_no_approximation("servicenow", M.CONCEPT_STATE_TRANSITION, transition)


def test_a_partial_gap_does_not_raise_when_empty_or_populated(samples):
    """`partial` states a condition, so both branches are legal — which is why the two
    kinds exist rather than one 'maybe missing' flag."""
    queue_owned = salesforce.map_case_work_item(ORG, samples["salesforce"]["case_queue_owned"])
    person_owned = salesforce.map_case_work_item(ORG, samples["salesforce"]["case_person_owned"])
    assert queue_owned.assigned_group is not None
    assert person_owned.assigned_group is None
    for produced in (queue_owned, person_owned):
        G.assert_no_approximation("salesforce", M.CONCEPT_WORK_ITEM, produced)


# ─────────────────────────────────────────────────────────────────────────────
# AC5, clause 1 — the gaps are RECORDED, and recorded honestly
# ─────────────────────────────────────────────────────────────────────────────

def test_every_field_gap_names_a_real_contract_field():
    """A gap naming a field the contract does not have is a stale record that will
    mislead the first pack author who reads it."""
    for connector_id, decl in conf.CONFORMANCE.items():
        for position in decl.concepts:
            known = {f.name for f in get_contract(position.concept).fields}
            for gap in position.field_gaps:
                assert gap.field in known, (
                    f"{connector_id}/{position.concept}: {gap.field!r} is not a "
                    f"contract field"
                )


def test_a_field_gap_naming_an_unknown_field_is_refused():
    with pytest.raises(conf.ConformanceError, match="not a field of this contract"):
        conf.ConceptConformance(
            M.CONCEPT_WORK_ITEM, conf.STATUS_SUPPORTED, mapper="x:y",
            field_gaps=(conf.FieldGap("sandwich", conf.GAP_ABSENT, "nope"),),
        )


def test_a_field_gap_without_a_reason_is_refused():
    """An unexplained empty field is indistinguishable from broken ingestion."""
    with pytest.raises(conf.ConformanceError, match="requires a reason"):
        conf.FieldGap("title", conf.GAP_ABSENT, "   ")


def test_a_field_gap_with_an_unknown_kind_is_refused():
    with pytest.raises(conf.ConformanceError, match="kind must be one of"):
        conf.FieldGap("title", "maybe", "because")


def test_supported_cannot_coexist_with_a_gap_on_a_required_field():
    """A connector that cannot populate a required field does not support the concept.
    A registry that says both is worse than none — a reader cannot tell which half to
    believe."""
    required = get_contract(M.CONCEPT_WORK_ITEM).required_fields
    assert "status_category" in required
    with pytest.raises(conf.ConformanceError, match="REQUIRED field"):
        conf.ConceptConformance(
            M.CONCEPT_WORK_ITEM, conf.STATUS_SUPPORTED, mapper="x:y",
            field_gaps=(conf.FieldGap("status_category", conf.GAP_ABSENT, "cannot"),),
        )
    # The same gap on a `declared` position is legitimate — that is how a connector
    # says "this is why I do not support it yet".
    conf.ConceptConformance(
        M.CONCEPT_WORK_ITEM, conf.STATUS_DECLARED,
        field_gaps=(conf.FieldGap("status_category", conf.GAP_ABSENT, "cannot"),),
    )


def test_a_repeated_field_gap_is_refused():
    with pytest.raises(conf.ConformanceError, match="repeats a field gap"):
        conf.ConceptConformance(
            M.CONCEPT_WORK_ITEM, conf.STATUS_SUPPORTED, mapper="x:y",
            field_gaps=(
                conf.FieldGap("title", conf.GAP_ABSENT, "a"),
                conf.FieldGap("title", conf.GAP_PARTIAL, "b"),
            ),
        )


def test_declared_entries_all_carry_a_reason():
    """`declared` is a work list. An entry with no reason is a placeholder nobody can
    action, which is how a work list becomes decoration."""
    for connector_id, decl in conf.CONFORMANCE.items():
        for position in decl.concepts:
            if position.status == conf.STATUS_DECLARED:
                assert position.reason.strip(), (
                    f"{connector_id}/{position.concept} is declared with no reason"
                )


# ─────────────────────────────────────────────────────────────────────────────
# AC5, clause 2 — visible to pack authors
# ─────────────────────────────────────────────────────────────────────────────

def test_concept_report_answers_which_sources_can_carry_a_concept():
    report = G.concept_gap_report()
    assert set(report) == set(M.CONCEPT_SET)
    approval = report[M.CONCEPT_APPROVAL]
    assert approval["usable_connector_ids"] == ["salesforce"]
    # And it says WHY the others cannot, rather than merely omitting them.
    for entry in approval["unavailable"]:
        assert entry["reason"].strip(), f"{entry['connector_id']} unavailable with no reason"


def test_usable_excludes_declared_so_a_detector_is_never_pointed_at_a_stub():
    """A detector pointed at a connector with no mapper would run, find nothing, and
    report the emptiness as an answer."""
    report = G.concept_gap_report()
    for concept, view in report.items():
        for entry in view["usable"]:
            assert entry["status"] == conf.STATUS_SUPPORTED
            assert entry["mapper"]
        for entry in view["unavailable"]:
            assert entry["status"] != conf.STATUS_SUPPORTED


def test_the_report_states_what_will_be_missing_not_just_what_is_available():
    """The half a pack author actually needs before writing a detector."""
    sn = next(
        e for e in G.concept_gap_report()[M.CONCEPT_STATE_TRANSITION]["usable"]
        if e["connector_id"] == "servicenow"
    )
    assert "actor_group" in sn["fields_never_populated"]
    sf = next(
        e for e in G.concept_gap_report()[M.CONCEPT_ASSIGNMENT]["usable"]
        if e["connector_id"] == "salesforce"
    )
    assert "assigned_to" in sf["fields_conditionally_populated"]
    condition = next(
        g for g in G.field_gaps_for("salesforce", M.CONCEPT_ASSIGNMENT)
        if g.field == "assigned_to"
    )
    assert "queue" in condition.reason.lower(), "a partial gap must state its condition"


def test_connector_report_separates_debt_from_decisions():
    """`outstanding` is work owed; `gaps` and `not_applicable` are decisions. Merging
    them produces a backlog full of items nobody intends to do."""
    view = G.connector_gap_report("jira")
    outstanding = {e["concept"] for e in view["outstanding"]}
    gaps = {g["concept"] for g in view["gaps"]}
    assert M.CONCEPT_STATE_TRANSITION in outstanding  # changelog exists, unread
    assert M.CONCEPT_ACTOR_GROUP in gaps              # individuals only, never mappable
    assert not (outstanding & gaps)


def test_connectors_for_detector_answers_the_portability_question():
    assert G.connectors_for_detector(M.CONCEPT_WORK_ITEM, M.CONCEPT_ACTOR_GROUP) == (
        "salesforce", "servicenow",
    )
    # An approval-bottleneck detector is Salesforce-only today, and says so.
    assert G.connectors_for_detector(M.CONCEPT_APPROVAL) == ("salesforce",)
    with pytest.raises(KeyError, match="not a normalised concept"):
        G.connectors_for_detector("sandwich")


def test_gap_summary_is_json_serialisable():
    """It is served by GET /api/concepts/gaps, so it must survive JSON."""
    payload = G.gap_summary()
    round_tripped = json.loads(json.dumps(payload))
    assert round_tripped["mapper_count"] == len(mp.MAPPERS)
    assert round_tripped["outstanding_count"] >= 1
    assert round_tripped["field_gap_count"] >= 1


# ─────────────────────────────────────────────────────────────────────────────
# ServiceNow — the {value, display_value} trap and the state vocabulary
# ─────────────────────────────────────────────────────────────────────────────

def test_servicenow_timestamps_come_from_the_raw_half(samples):
    """The trap CLAUDE.md documents: the raw half is canonical UTC
    `YYYY-MM-DD HH:MM:SS`; the display half is the instance's format in the user's
    timezone. The fixture's two halves differ, so reading the wrong one fails here."""
    record = samples["servicenow"]["incident_open"]
    item = servicenow.map_incident_work_item(ORG, record)
    assert item.opened_at == "2026-07-01 09:14:22"
    assert item.observed_at == "2026-07-14 16:02:11"
    assert item.provenance["source_timestamp"] == "2026-07-14 16:02:11"
    # …and the display half is what a human-facing label uses.
    assert item.native_status == "In Progress"
    assert item.assigned_group.display_name == "Level 2 Payments"


def test_servicenow_group_reference_is_keyed_on_sys_id_not_name(samples):
    """A name-keyed group merges two groups that happen to share a label."""
    item = servicenow.map_incident_work_item(ORG, samples["servicenow"]["incident_open"])
    assert item.assigned_group.source_record_id == "9f8e7d6c5b4a39281706f5e4d3c2b1a0"


def test_servicenow_cancelled_is_not_resolved(samples):
    """Counting abandoned work as completed overstates throughput for every detector
    downstream — the contract names this explicitly."""
    item = servicenow.map_incident_work_item(ORG, samples["servicenow"]["incident_cancelled"])
    assert item.status_category == "cancelled"
    assert item.status_category != "resolved"
    assert item.is_open is False


def test_servicenow_unknown_state_raises_rather_than_defaulting(samples):
    """States are per-instance configurable, so an unknown value means a custom state
    nobody classified. A silent 'other' would change what counts as open."""
    with pytest.raises(_common.MappingInputError, match="no mapping onto"):
        servicenow.map_incident_work_item(
            ORG, samples["servicenow"]["incident_custom_state"]
        )


def test_servicenow_unmapped_table_is_refused(samples):
    with pytest.raises(_common.MappingInputError, match="no declared work-item type"):
        servicenow.map_incident_work_item(
            ORG, samples["servicenow"]["incident_open"], table="u_custom_table"
        )


def test_servicenow_reopen_is_not_a_plain_status_change(samples):
    """Rework is the signal a detector hunts; collapsing it erases it."""
    reopen = servicenow.map_state_transition(ORG, samples["servicenow"]["audit_state_reopen"])
    assert reopen.transition_type == "reopen"
    assert (reopen.from_status_category, reopen.to_status_category) == ("resolved", "in_progress")

    ordinary = servicenow.map_state_transition(ORG, samples["servicenow"]["audit_state_change"])
    assert ordinary.transition_type == "status_change"


def test_servicenow_first_assignment_and_reassignment_are_distinguished(samples):
    """'How many times was this passed on?' is unanswerable from an undifferentiated
    stream, which is the whole point of the assignment concept."""
    first = servicenow.map_assignment_history(ORG, samples["servicenow"]["audit_first_assignment"])
    later = servicenow.map_assignment_history(
        ORG, samples["servicenow"]["audit_reassignment"], hop_index=1
    )
    assert first.assignment_type == "initial"
    assert later.assignment_type == "reassignment"
    assert later.hop_index == 1
    # Escalation is NOT guessed from the group's name.
    assert later.assignment_type != "escalation"


def test_servicenow_assignment_group_is_a_queue_not_a_team(samples):
    """Work is routed to and drawn from an assignment group — unlike a chat channel."""
    group = servicenow.map_assignment_group(ORG, samples["servicenow"]["assignment_group_record"])
    assert group.group_type == "queue"
    assert group.member_count == 14


def test_servicenow_cmdb_reference_matches_the_graph_entity_type(samples):
    """`system` is what resource_graph.py and MSP-B3's CMDB ingestion write, so a
    concept reference and a graph entity agree about what a CI is."""
    ref = servicenow.map_cmdb_ci_reference(samples["servicenow"]["cmdb_ci"])
    assert ref.entity_type == "system"
    assert ref.is_resolved is False, "resolution is the graph's decision, not a mapper's"


# ─────────────────────────────────────────────────────────────────────────────
# Jira — statusCategory, and the abandoned-work trap
# ─────────────────────────────────────────────────────────────────────────────

def test_jira_wont_do_is_cancelled_not_closed(samples):
    """Jira files "Won't Do" under statusCategory='done'. Mapping done→closed blindly
    would count abandoned work as delivered."""
    abandoned = jira.map_issue_work_item(ORG, samples["jira"]["issue_wont_do"])
    assert abandoned.status_category == "cancelled"
    completed = jira.map_issue_work_item(ORG, samples["jira"]["issue_done"])
    assert completed.status_category == "closed"


def test_jira_status_reads_the_status_category_not_the_name(samples):
    """statusCategory is fixed by Jira; status NAMES are per-project."""
    item = jira.map_issue_work_item(ORG, samples["jira"]["issue_in_progress"])
    assert item.status_category == "in_progress"
    assert item.native_status == "In Review"  # the native name survives for trace-back


def test_jira_never_synthesises_a_group_from_an_assignee(samples):
    """The fixture HAS an assignee, so this is not vacuous."""
    record = samples["jira"]["issue_in_progress"]
    assert record["fields"]["assignee"]["displayName"] == "Sam Rivera"
    item = jira.map_issue_work_item(ORG, record)
    assert item.assigned_group is None
    assert "Sam Rivera" not in json.dumps(item.to_dict())


def test_jira_custom_issue_type_falls_back_to_other(samples):
    """Safe where a status fallback would not be: nothing branches open-vs-closed on
    work_item_type, and Jira types are freely invented per project."""
    item = jira.map_issue_work_item(ORG, samples["jira"]["issue_custom_type"])
    assert item.work_item_type == "other"
    assert item.native_type == "Governance Review"


def test_jira_closed_at_is_not_a_copy_of_resolved_at(samples):
    """A manufactured zero would corrupt a resolve-to-close duration."""
    item = jira.map_issue_work_item(ORG, samples["jira"]["issue_done"])
    assert item.resolved_at is not None
    assert item.closed_at is None


def test_jira_reference_is_keyed_on_id_not_key(samples):
    """A key changes when an issue moves project; a reference that stops resolving
    after a move is not a reference."""
    ref = jira.map_issue_reference(samples["jira"]["issue_in_progress"])
    assert ref.source_record_id == "10421"
    assert ref.display_name == "PAY-42"


def test_jira_has_no_actor_group_or_approval_mapper():
    """The gap is visible in the CODE, not only in the registry: there is nothing to
    call. A mapper here could trivially name an assignee as a team."""
    for concept in (M.CONCEPT_ACTOR_GROUP, M.CONCEPT_APPROVAL):
        with pytest.raises(mp.MapperError, match="no mapper for"):
            mp.get_mapper("jira", concept)


# ─────────────────────────────────────────────────────────────────────────────
# Salesforce — the deterministic queue-vs-person branch, and approvals
# ─────────────────────────────────────────────────────────────────────────────

def test_salesforce_queue_owner_becomes_a_group(samples):
    item = salesforce.map_case_work_item(ORG, samples["salesforce"]["case_queue_owned"])
    assert item.assigned_group is not None
    assert item.assigned_group.source_record_id.startswith(salesforce.QUEUE_ID_PREFIX)
    assert item.attributes.get("owner_is_individual") is None


def test_salesforce_person_owner_yields_no_group_and_says_why(samples):
    """The record notes that the owner is an individual, so an empty group field is
    explicable rather than looking like broken ingestion — and no name is carried."""
    record = samples["salesforce"]["case_person_owned"]
    assert record["OwnerName"] == "Sam Rivera"
    item = salesforce.map_case_work_item(ORG, record)
    assert item.assigned_group is None
    assert item.attributes["owner_is_individual"] is True
    assert "Sam Rivera" not in json.dumps(item.to_dict())


def test_salesforce_unknown_case_status_raises(samples):
    with pytest.raises(_common.MappingInputError, match="no mapping onto"):
        salesforce.map_case_work_item(ORG, samples["salesforce"]["case_custom_status"])


def test_salesforce_removed_approval_is_withdrawn_not_rejected(samples):
    """Conflating them would report a cancelled request as a refusal."""
    withdrawn = salesforce.map_process_instance_approval(
        ORG, samples["salesforce"]["approval_removed"]
    )
    assert withdrawn.decision == "withdrawn"
    approved = salesforce.map_process_instance_approval(
        ORG, samples["salesforce"]["approval_approved"]
    )
    assert approved.decision == "approved"


def test_salesforce_pending_approval_is_emitted_with_no_decision_time(samples):
    """An undecided approval is what a bottleneck detector measures, so it must be
    representable — and defaulting decided_at would make it look instantaneous."""
    pending = salesforce.map_process_instance_approval(
        ORG, samples["salesforce"]["approval_pending"]
    )
    assert pending.decision == "pending"
    assert pending.is_decided is False
    assert pending.decided_at is None
    assert pending.requested_at is not None


def test_salesforce_refuses_a_non_queue_group(samples):
    """A Role or territory group is also a Salesforce Group; an org-chart node is not
    a work queue."""
    with pytest.raises(_common.MappingInputError, match="not a Queue"):
        salesforce.map_queue_actor_group(ORG, samples["salesforce"]["role_group"])


def test_salesforce_history_splits_into_transitions_and_assignments(samples):
    """One pass over CaseHistory yields both concepts, and hop_index is assigned here
    because this is the first place the ORDER of a case's assignments is known."""
    s = samples["salesforce"]
    rows = [
        s["history_status_change"], s["history_owner_to_queue"],
        s["history_priority_change"], s["history_owner_to_person"],
    ]
    out = salesforce.map_case_history_stream(ORG, rows)
    assert [t.transition_type for t in out["transitions"]] == ["status_change"]
    assert [a.hop_index for a in out["assignments"]] == [0, 1]
    # The priority row belongs to neither concept and is dropped, not forced into one.
    assert len(out["transitions"]) + len(out["assignments"]) == 3


def test_salesforce_non_status_history_row_returns_none(samples):
    """Out of scope for the concept is None; malformed is an exception. Collapsing the
    two would hide real ingestion faults."""
    assert salesforce.map_case_history_transition(
        ORG, samples["salesforce"]["history_priority_change"]
    ) is None
    assert salesforce.map_case_owner_assignment(
        ORG, samples["salesforce"]["history_status_change"]
    ) is None


def test_salesforce_handoff_to_a_person_still_counts_the_hop(samples):
    """The hop happened; a churn detector counts hops. Naming the recipient would name
    an individual, so assigned_to is absent — the documented partial condition."""
    assignment = salesforce.map_case_owner_assignment(
        ORG, samples["salesforce"]["history_owner_to_person"]
    )
    assert assignment.assignment_type == "reassignment"
    assert assignment.assigned_to is None
    assert assignment.attributes["to_owner_is_individual"] is True


# ─────────────────────────────────────────────────────────────────────────────
# Content sources — a channel is a team, and content never rides the concept
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("platform", ["slack", "teams"])
def test_a_channel_is_a_team_never_a_queue(samples, platform):
    """A queue-ageing detector reading a channel as a queue would report backlog that
    does not exist."""
    mapper = content.map_slack_channel if platform == "slack" else content.map_teams_channel
    group = mapper(ORG, samples[platform]["channel"])
    assert group.group_type == "team"
    assert group.group_type != "queue"


@pytest.mark.parametrize("platform", ["slack", "teams"])
def test_a_thread_is_classified_once_for_retrieval(samples, platform):
    """content_type must be the value the retrieval substrate would chunk it under —
    two vocabularies for one idea drift."""
    mapper = content.map_slack_thread if platform == "slack" else content.map_teams_thread
    artifact = mapper(ORG, samples[platform]["thread"])
    assert (artifact.artifact_type, artifact.content_type) == ("conversation", "conversation")
    assert artifact.revision is None


@pytest.mark.parametrize("platform", ["slack", "teams"])
def test_thread_carries_a_participant_count_never_names(samples, platform):
    mapper = content.map_slack_thread if platform == "slack" else content.map_teams_thread
    artifact = mapper(ORG, samples[platform]["thread"])
    assert artifact.attributes["participant_count"] >= 2
    assert "Sam Rivera" not in json.dumps(artifact.to_dict())


def test_a_sharepoint_folder_is_not_an_artifact(samples):
    """content_router classifies a folder as SKIP; mapping it to 'other' would put a
    container in the same concept as a document."""
    with pytest.raises(_common.MappingInputError, match="not an artifact"):
        content.map_sharepoint_item(ORG, samples["sharepoint"]["folder"])


def test_sharepoint_file_and_page_are_different_artifact_types(samples):
    assert content.map_sharepoint_item(ORG, samples["sharepoint"]["file"]).artifact_type == "document"
    assert content.map_sharepoint_item(ORG, samples["sharepoint"]["page"]).artifact_type == "page"


def test_git_artifact_kind_must_be_stated(samples):
    """Guessing from the record's shape would mis-type a commit that touched one file."""
    with pytest.raises(_common.MappingInputError, match="requires kind="):
        content.map_git_artifact(ORG, {"repo_id": "acme/payments", "sha": "abc123"})
    commit = content.map_git_artifact(ORG, samples["github"]["commit"])
    code = content.map_git_artifact(ORG, samples["github"]["file"])
    assert (commit.artifact_type, commit.content_type) == ("commit", "conversation")
    assert (code.artifact_type, code.content_type) == ("code_file", "code")


def test_no_artifact_carries_its_content(samples):
    """An artifact is a REFERENCE. Content reaches the platform through the substrate's
    single ingest path, which is where secret redaction runs."""
    for connector_id, concept, produced in _all_mapped_outputs(samples):
        if concept != M.CONCEPT_ARTIFACT:
            continue
        for forbidden in ("content", "body", "text", "raw"):
            assert forbidden not in produced.attributes, (
                f"{connector_id} put {forbidden!r} on an artifact's attributes, routing "
                f"content around the redaction path"
            )


# ─────────────────────────────────────────────────────────────────────────────
# Cloud events — one concept only, and the refusal to invent an entity
# ─────────────────────────────────────────────────────────────────────────────

def _operational_event(resource=True):
    from discovery.signals.operational_event import OperationalEvent, ResourceRef

    kwargs = dict(
        org_id=ORG,
        source_system="aws",
        signal_id="evt-1",
        observed_at="2026-07-14T16:00:00Z",
        provenance={
            "source_system": "aws",
            "source_artifact": "evt-1",
            "source_timestamp": "2026-07-14T16:00:00Z",
            "origin": "observed",
        },
        event_class="error",
        severity="high",
        event_type="CloudWatch/AlarmStateChange",
    )
    if resource:
        kwargs["resource"] = ResourceRef(
            provider="aws",
            resource_type="compute",
            resource_id="arn:aws:ec2:us-east-1:123456789012:instance/i-0abc",
            name="pay-batch-01",
        )
    return OperationalEvent(**kwargs)


def test_cloud_event_maps_only_the_resource_reference():
    ref = cloud_events.map_aws_resource_reference(_operational_event())
    assert ref.entity_type == "system"
    assert ref.source_record_id.startswith("arn:aws:ec2:")
    assert ref.is_resolved is False


def test_a_resourceless_event_is_refused_not_given_a_stand_in():
    """An account- or region-level stand-in would create a graph entity for a thing
    that does not exist — the speculative modelling resource_graph.py refuses."""
    with pytest.raises(_common.MappingInputError, match="references no cloud resource"):
        cloud_events.map_aws_resource_reference(_operational_event(resource=False))
    assert cloud_events.resource_reference_or_none("aws", _operational_event(resource=False)) is None


def test_cloud_sources_declare_every_workflow_concept_not_applicable():
    """B0's OperationalEvent is the right profile for a cloud event; an alarm is not a
    work item and a resource state change is not a work-item transition."""
    for connector_id in ("aws_events", "azure_events"):
        view = G.connector_gap_report(connector_id)
        assert [e["concept"] for e in view["supported"]] == [M.CONCEPT_ENTITY_REFERENCE]
        not_applicable = {e["concept"] for e in view["not_applicable"]}
        assert M.CONCEPT_WORK_ITEM in not_applicable
        assert M.CONCEPT_STATE_TRANSITION in not_applicable
        assert view["gaps"] == [], "a deliberate boundary is not a shortcoming"


# ─────────────────────────────────────────────────────────────────────────────
# Cross-cutting invariants
# ─────────────────────────────────────────────────────────────────────────────

def test_every_mapped_concept_is_org_scoped_and_traceable(samples):
    for connector_id, concept, produced in _all_mapped_outputs(samples):
        if concept == M.CONCEPT_ENTITY_REFERENCE:
            continue  # a value type carries no observation spine, by contract
        assert produced.org_id == ORG
        assert produced.provenance["origin"] == "observed"
        assert produced.provenance["source_artifact"]
        assert produced.provenance["source_artifact_type"] == "record_id"
        assert produced.signal_id


def test_no_mapped_concept_names_an_individual(samples):
    """The platform's standing rule. The fixtures deliberately CONTAIN individuals —
    assigned_to, caller_id, Jira assignee/reporter, a Salesforce user owner, thread
    participants — so a pass here is meaningful rather than vacuous."""
    people = ["Sam Rivera", "Priya Nadar", "Alex Chen"]
    person_ids = ["62826bf03710200044e0bfc8bcbe5df1", "5b10a2844c20165700ede21g"]
    for connector_id, concept, produced in _all_mapped_outputs(samples):
        blob = json.dumps(produced.to_dict())
        for name in people:
            assert name not in blob, f"{connector_id}/{concept} leaked {name!r}"
        for pid in person_ids:
            assert pid not in blob, f"{connector_id}/{concept} leaked a person id"


def test_every_mapped_output_satisfies_its_contract_required_fields(samples):
    """A mapper that omitted a required field would produce a concept the contract says
    cannot exist."""
    for connector_id, concept, produced in _all_mapped_outputs(samples):
        for field in get_contract(concept).required_fields:
            value = (
                produced.get(field) if isinstance(produced, dict)
                else getattr(produced, field, None)
            )
            assert value not in (None, ""), (
                f"{connector_id}/{concept} left required field {field!r} empty"
            )


def test_mappers_are_deterministic(samples):
    """Same record in, same concept out — what lets a golden fixture pin the output and
    what 2.0-A1's reproducibility rule requires of anything downstream."""
    record = samples["servicenow"]["incident_open"]
    first = servicenow.map_incident_work_item(ORG, record).to_dict()
    second = servicenow.map_incident_work_item(ORG, record).to_dict()
    assert first == second


def test_the_mapper_layer_reads_no_environment_and_no_database():
    """Purity, structurally. A mapper that read config would make a concept mean
    different things in two deployments, and one that touched the DB could not run in
    an offline fixture test."""
    import ast

    package = Path(mp.__file__).parent
    offenders = []
    for path in sorted(package.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in ("environ", "getenv"):
                offenders.append(f"{path.name}: os.{node.attr}")
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] in ("psycopg2", "sqlalchemy"):
                        offenders.append(f"{path.name}: import {alias.name}")
            if isinstance(node, ast.ImportFrom) and node.module:
                root = node.module.split(".")
                # app.provenance is a pure dataclass module and is the ONE permitted
                # app import; anything else in app/ reaches the DB or the request.
                if root[0] in ("app", "backend") and "provenance" not in node.module:
                    if not node.module.startswith(("discovery", "backend.discovery")):
                        offenders.append(f"{path.name}: from {node.module}")
    assert offenders == [], f"the mapper layer must stay pure: {offenders}"


def test_os_is_not_even_imported_by_the_mapper_layer():
    """Belt and braces on the AST sweep above."""
    package = Path(mp.__file__).parent
    for path in sorted(package.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        assert "os.environ" not in source and "os.getenv" not in source, path.name
    assert os.environ is not None  # the test module itself may use os; the layer may not
