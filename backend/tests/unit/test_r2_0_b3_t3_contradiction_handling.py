"""2.0-B3 T3 — contradiction handling: name the disagreement, never resolve it.

AC3: "Seeded contradictory sources produce a finding that names the disagreement
rather than silently resolving it."

Three properties carry the story, and the tests are grouped by them:

  * **the disagreement is NAMED** — seeded conflicting sources reach the finding with
    both values and both source systems present in the rendered copy;
  * **it is never RESOLVED** — detection is structurally incapable of choosing a
    side (it returns a record and mutates nothing), the copy guard blocks wording
    that would settle it, and a budget trim cannot quietly decide the argument
    either;
  * **it is MATERIAL** — formatting differences, absent values, numbers inside
    tolerance and one system disagreeing with itself are all NOT contradictions. A
    detector that cries wolf is a detector people switch off.

DB-free: detection is pure.
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from app import context_contradictions as cc
from app.context_assembly import (
    AssemblyPolicy,
    Candidate,
    KIND_ENTITY,
    KIND_EVIDENCE,
    assemble_context,
)
from app.assembly_policy_config import (
    AssemblyPolicyConfigError,
    parse_declared_policy,
)


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------

def entity(cid, subject, source_system, origin="observed", **attributes):
    """An entity candidate stating ``attributes`` about ``subject``."""
    payload = {
        "entity_id": cid,
        "display_name": subject,
        "source_system": source_system,
        "metadata": dict(attributes),
    }
    return Candidate(
        candidate_id=cid,
        kind=KIND_ENTITY,
        origin=origin,
        confidence=0.9,
        source_type="structured",
        payload=payload,
    )


def doc(cid, subject, source_system, origin="observed", **attributes):
    """An evidence chunk whose producer indexed a STRUCTURED claim alongside prose."""
    payload = {
        "chunk_id": cid,
        "source_system": source_system,
        "content_type": "prose",
        "provenance": {"subject": subject, **attributes},
    }
    return Candidate(
        candidate_id=cid,
        kind=KIND_EVIDENCE,
        origin=origin,
        confidence=0.7,
        source_type="prose",
        payload=payload,
    )


CMDB_OWNER = entity("e-cmdb", "Payments API", "servicenow", owner="Platform Engineering")
RUNBOOK_OWNER = doc("c-runbook", "Payments API", "confluence", owner="L2 Support")


# ---------------------------------------------------------------------------
# The disagreement is NAMED
# ---------------------------------------------------------------------------

class TestTheDisagreementIsNamed:
    def test_seeded_contradiction_is_detected(self):
        report = cc.detect_contradictions([CMDB_OWNER, RUNBOOK_OWNER])
        assert report.any_found
        assert len(report.contradictions) == 1
        found = report.contradictions[0]
        assert found.subject == "Payments API"
        assert found.attribute == "owner"
        assert set(found.source_systems) == {"servicenow", "confluence"}
        assert found.distinct_values == 2

    def test_summary_names_both_values_and_both_sources(self):
        found = cc.detect_contradictions([CMDB_OWNER, RUNBOOK_OWNER]).contradictions[0]
        summary = found.summary
        # Every side survives into the copy — the whole point of the story.
        assert "Platform Engineering" in summary
        assert "L2 Support" in summary
        assert "servicenow" in summary
        assert "confluence" in summary
        assert "Payments API" in summary
        assert "owner" in summary

    def test_summary_states_that_the_platform_does_not_choose(self):
        found = cc.detect_contradictions([CMDB_OWNER, RUNBOOK_OWNER]).contradictions[0]
        assert "does not choose between them" in found.summary

    def test_rendered_section_instructs_the_model_not_to_resolve(self):
        report = cc.detect_contradictions([CMDB_OWNER, RUNBOOK_OWNER])
        section = cc.render_contradiction_section(report)
        assert "Do not choose a side" in section
        assert "Platform Engineering" in section and "L2 Support" in section

    def test_no_disagreement_renders_nothing(self):
        agree = entity("e2", "Payments API", "jira", owner="Platform Engineering")
        report = cc.detect_contradictions([CMDB_OWNER, agree])
        assert not report.any_found
        assert cc.render_contradiction_section(report) == ""

    def test_serialised_report_renders_the_same_copy(self):
        """A consumer holding to_dict() must render exactly what the object would —
        otherwise every surface reinvents the sentence and one of them gets it wrong."""
        report = cc.detect_contradictions([CMDB_OWNER, RUNBOOK_OWNER])
        assert cc.render_reported_section(report.to_dict()) == (
            cc.render_contradiction_section(report)
        )

    def test_several_attributes_of_one_subject_each_report(self):
        a = entity("e-a", "Payments API", "servicenow", owner="Team A", state="Operational")
        b = entity("e-b", "Payments API", "cmdb", owner="Team B", state="Retired")
        report = cc.detect_contradictions([a, b])
        assert {c.attribute for c in report.contradictions} == {"owner", "state"}


# ---------------------------------------------------------------------------
# It is never RESOLVED
# ---------------------------------------------------------------------------

class TestItIsNeverResolved:
    def test_both_positions_survive_in_the_record(self):
        found = cc.detect_contradictions([CMDB_OWNER, RUNBOOK_OWNER]).contradictions[0]
        assert len(found.positions) == 2
        assert {p.value for p in found.positions} == {"Platform Engineering", "L2 Support"}

    def test_the_record_carries_no_winner_field(self):
        """No 'preferred', 'resolved_value', 'winner' or severity — there is nothing
        here to rank the sources by, and a scale would be winner-picking as a number."""
        found = cc.detect_contradictions([CMDB_OWNER, RUNBOOK_OWNER]).contradictions[0]
        serialised = found.to_dict()
        for forbidden in (
            "winner", "preferred", "resolved_value", "chosen", "authoritative",
            "severity", "score", "confidence",
        ):
            assert forbidden not in serialised

    def test_detection_does_not_mutate_or_reorder_its_input(self):
        candidates = [CMDB_OWNER, RUNBOOK_OWNER]
        before = list(candidates)
        cc.detect_contradictions(candidates)
        assert candidates == before
        assert candidates[0] is CMDB_OWNER and candidates[1] is RUNBOOK_OWNER

    def test_resolution_language_is_refused_at_build_time(self):
        for phrase in ("the correct owner is Team A", "Team B should be the owner"):
            assert cc.resolution_language_in(phrase)
            with pytest.raises(cc.ContradictionCopyError):
                cc.assert_no_resolution(phrase)

    def test_the_guard_does_not_flag_the_evidence_itself(self):
        """A guard that flags the values it is reporting trains people to ignore it."""
        found = cc.detect_contradictions([CMDB_OWNER, RUNBOOK_OWNER]).contradictions[0]
        assert cc.resolution_language_in(found.summary) == []

    def test_module_never_assigns_to_a_selection_structure(self):
        """Structural, not conventional: walk the AST and fail the build if this module
        ever writes to something that would re-rank or re-select candidates."""
        source = Path(inspect.getfile(cc)).read_text(encoding="utf-8")
        forbidden = {"selected", "ordered", "ranking", "rank_key", "eligible", "candidates"}
        offenders = []
        for node in ast.walk(ast.parse(source)):
            targets = []
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
                targets = [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and target.id in forbidden:
                    offenders.append(target.id)
                if isinstance(target, (ast.Subscript, ast.Attribute)):
                    base = target.value
                    if isinstance(base, ast.Name) and base.id in forbidden:
                        offenders.append(base.id)
        assert not offenders, (
            f"context_contradictions assigns to selection state {sorted(set(offenders))}"
            f" — detection must append a record, never influence what was selected"
        )

    def test_a_budget_trim_cannot_settle_the_argument(self):
        """One side dropped by the budget is still REPORTED, flagged out-of-context.

        Were detection to read only the selected set, a narrow budget would silently
        resolve the disagreement — the story's failure mode, one layer down.
        """
        report = cc.detect_contradictions(
            [CMDB_OWNER, RUNBOOK_OWNER], selected_ids={"e-cmdb"}
        )
        found = report.contradictions[0]
        assert not found.fully_in_context
        by_id = {p.candidate_id: p for p in found.positions}
        assert by_id["e-cmdb"].in_context is True
        assert by_id["c-runbook"].in_context is False
        assert "outside this finding's context budget" in found.summary


# ---------------------------------------------------------------------------
# It is MATERIAL
# ---------------------------------------------------------------------------

class TestMateriality:
    @pytest.mark.parametrize(
        "other_spelling",
        ["Platform Engineering", "platform engineering", "PLATFORM_ENGINEERING",
         "platform-engineering", " Platform  Engineering "],
    )
    def test_formatting_differences_are_not_disagreements(self, other_spelling):
        other = doc("c-x", "Payments API", "confluence", owner=other_spelling)
        assert not cc.detect_contradictions([CMDB_OWNER, other]).any_found

    def test_subject_matching_is_normalised_too(self):
        """'payments-api' and 'Payments API' are one subject; otherwise the same
        disagreement would present as two subjects that each agree with themselves."""
        other = doc("c-x", "payments-api", "confluence", owner="L2 Support")
        assert cc.detect_contradictions([CMDB_OWNER, other]).any_found

    def test_a_missing_value_is_not_a_position(self):
        silent = doc("c-silent", "Payments API", "confluence", owner="")
        assert not cc.detect_contradictions([CMDB_OWNER, silent]).any_found
        absent = doc("c-absent", "Payments API", "confluence", state="Operational")
        assert not cc.detect_contradictions([CMDB_OWNER, absent]).any_found

    def test_one_system_disagreeing_with_itself_is_not_reported(self):
        """A data-quality problem inside one system, not a cross-source disagreement."""
        a = entity("e-1", "Payments API", "servicenow", owner="Team A")
        b = entity("e-2", "Payments API", "servicenow", owner="Team B")
        assert not cc.detect_contradictions([a, b]).any_found

    def test_declared_equivalences_collapse_two_spellings(self):
        policy = cc.ContradictionPolicy(
            comparable_attributes=(
                cc.ComparableAttribute(
                    name="owner",
                    equivalences=(("L2 Support", "Level 2 Support", "Tier 2"),),
                ),
            )
        )
        other = doc("c-x", "Payments API", "confluence", owner="Level 2 Support")
        pair = [RUNBOOK_OWNER, other]
        assert not cc.detect_contradictions(pair, policy).any_found
        # ...and a genuinely different value still reports.
        assert cc.detect_contradictions([RUNBOOK_OWNER, CMDB_OWNER], policy).any_found

    def test_numeric_values_inside_tolerance_agree(self):
        policy = cc.ContradictionPolicy(
            comparable_attributes=(
                cc.ComparableAttribute(name="sla_hours", kind=cc.KIND_NUMERIC),
            ),
            numeric_tolerance_ratio=0.10,
        )
        a = entity("e-a", "Payments API", "servicenow", sla_hours="4.0")
        close = doc("c-b", "Payments API", "confluence", sla_hours="4.2")
        far = doc("c-c", "Payments API", "confluence", sla_hours="24")
        assert not cc.detect_contradictions([a, close], policy).any_found
        assert cc.detect_contradictions([a, far], policy).any_found

    def test_undeclared_attributes_are_never_compared(self):
        a = entity("e-a", "Payments API", "servicenow", cost_centre="CC-1")
        b = doc("c-b", "Payments API", "confluence", cost_centre="CC-9")
        assert not cc.detect_contradictions([a, b]).any_found

    def test_inferred_positions_do_not_make_a_source_disagreement(self):
        """The platform disagreeing with a source is not two sources disagreeing."""
        guess = doc("c-guess", "Payments API", "confluence", origin="inferred",
                    owner="L2 Support")
        assert not cc.detect_contradictions([CMDB_OWNER, guess]).any_found
        permissive = cc.ContradictionPolicy(
            comparable_attributes=cc.DEFAULT_COMPARABLE_ATTRIBUTES,
            require_observed=False,
        )
        assert cc.detect_contradictions([CMDB_OWNER, guess], permissive).any_found

    def test_narrative_text_is_never_parsed_for_claims(self):
        """A claim read out of a paragraph is an inference dressed as an observation."""
        prose = Candidate(
            candidate_id="c-prose",
            kind=KIND_EVIDENCE,
            origin="observed",
            source_type="prose",
            payload={
                "chunk_id": "c-prose",
                "source_system": "confluence",
                "provenance": {"subject": "Payments API"},
                "text": "The owner of the Payments API is L2 Support.",
            },
        )
        assert not cc.detect_contradictions([CMDB_OWNER, prose]).any_found


# ---------------------------------------------------------------------------
# Determinism and loud bounding
# ---------------------------------------------------------------------------

class TestDeterminismAndBounds:
    def test_same_inputs_in_any_order_produce_an_identical_report(self):
        a = entity("e-a", "Payments API", "servicenow", owner="Team A")
        b = doc("c-b", "Payments API", "confluence", owner="Team B")
        c = entity("e-c", "Billing", "cmdb", state="Retired")
        d = doc("c-d", "Billing", "confluence", state="Operational")
        forward = cc.detect_contradictions([a, b, c, d]).to_dict()
        reverse = cc.detect_contradictions([d, c, b, a]).to_dict()
        assert forward == reverse

    def test_omitted_disagreements_are_counted_not_hidden(self):
        policy = cc.ContradictionPolicy(
            comparable_attributes=cc.DEFAULT_COMPARABLE_ATTRIBUTES, max_reported=2
        )
        candidates = []
        for i in range(5):
            candidates.append(entity(f"e-{i}", f"Service {i}", "servicenow", owner="A"))
            candidates.append(doc(f"c-{i}", f"Service {i}", "confluence", owner="B"))
        report = cc.detect_contradictions(candidates, policy)
        assert report.total_detected == 5
        assert len(report.contradictions) == 2
        assert report.omitted == 3
        assert "3 further disagreement(s)" in cc.render_contradiction_section(report)


# ---------------------------------------------------------------------------
# Declared configuration
# ---------------------------------------------------------------------------

class TestDeclaredConfiguration:
    def test_shipped_declaration_parses_and_declares_owner(self):
        from app.assembly_policy_config import load_declared_policy

        declared = load_declared_policy()
        names = [a.name for a in declared.contradictions.comparable_attributes]
        assert "owner" in names and "state" in names

    def test_absent_block_falls_back_to_documented_defaults(self):
        """A config written before T3 still detects disagreements rather than
        shipping the feature quietly switched off."""
        declared = parse_declared_policy(_minimal_declaration())
        assert declared.contradictions.comparable_attributes == (
            cc.DEFAULT_COMPARABLE_ATTRIBUTES
        )

    @pytest.mark.parametrize(
        "block",
        [
            {"comparable_attributes": [{"name": "owner", "kind": "fuzzy"}]},
            {"comparable_attributes": [{"name": ""}]},
            {"comparable_attributes": [{"name": "owner"}, {"name": "owner"}]},
            {"numeric_tolerance_ratio": 1.5},
            {"min_distinct_sources": 1},
            {"max_reported": 0},
            {"require_observed": "yes"},
        ],
    )
    def test_a_present_but_invalid_block_raises(self, block):
        declaration = _minimal_declaration()
        declaration["contradictions"] = block
        with pytest.raises(AssemblyPolicyConfigError):
            parse_declared_policy(declaration)

    def test_declaration_records_the_contradiction_policy(self):
        """A stored selection_log has to be interpretable against the rules in force."""
        declared = parse_declared_policy(_minimal_declaration())
        assert "owner" in declared.to_dict()["contradictions"]["comparable_attributes"]


def _minimal_declaration():
    return {
        "version": 1,
        "budget_partitions": ["origin"],
        "ranking": ["confidence", "candidate_id"],
        "origin_ranks": {"observed": 0, "inferred": 1},
        "source_type_ranks": {"structured": 0, "conversation": 3},
    }


# ---------------------------------------------------------------------------
# End to end through the assembler
# ---------------------------------------------------------------------------

class TestThroughTheAssembler:
    def _graph(self):
        return {
            "entities": [
                {
                    "entity_id": "e-cmdb",
                    "display_name": "Payments API",
                    "source_system": "servicenow",
                    "confidence": 0.9,
                    "metadata": {"owner": "Platform Engineering"},
                },
                {
                    "entity_id": "e-runbook",
                    "display_name": "Payments API",
                    "source_system": "confluence",
                    "confidence": 0.8,
                    "metadata": {"owner": "L2 Support"},
                },
            ],
            "relationships": [],
        }

    def test_assembled_package_reports_the_disagreement(self):
        package = assemble_context(
            opportunity={"id": "opp-1"}, graph=self._graph(), policy=AssemblyPolicy()
        )
        assert len(package.contradictions) == 1
        found = package.contradictions[0]
        assert found["attribute"] == "owner"
        assert "Platform Engineering" in found["summary"]
        assert "L2 Support" in found["summary"]
        assert package.contradiction_report["detected"] == 1

    def test_agreeing_sources_report_nothing(self):
        graph = self._graph()
        graph["entities"][1]["metadata"]["owner"] = "Platform Engineering"
        package = assemble_context(
            opportunity={"id": "opp-1"}, graph=graph, policy=AssemblyPolicy()
        )
        assert package.contradictions == []
        assert package.contradiction_report["detected"] == 0

    def test_both_sides_still_reach_the_package(self):
        """Naming the disagreement must not cost either side its place in context."""
        package = assemble_context(
            opportunity={"id": "opp-1"}, graph=self._graph(), policy=AssemblyPolicy()
        )
        assert len(package.entities) == 2

    def test_a_budget_that_drops_one_side_still_names_the_disagreement(self):
        package = assemble_context(
            opportunity={"id": "opp-1"},
            graph=self._graph(),
            policy=AssemblyPolicy(max_entities=1),
        )
        assert len(package.entities) == 1
        found = package.contradictions[0]
        assert found["fully_in_context"] is False
        assert {p["in_context"] for p in found["positions"]} == {True, False}

    def test_detection_failure_degrades_and_never_breaks_assembly(self, monkeypatch):
        import app.context_assembly as ca

        monkeypatch.setattr(
            ca._contradictions,
            "detect_contradictions",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        package = assemble_context(
            opportunity={"id": "opp-1"}, graph=self._graph(), policy=AssemblyPolicy()
        )
        assert package.entities  # the finding kept its context
        assert package.contradictions == []
