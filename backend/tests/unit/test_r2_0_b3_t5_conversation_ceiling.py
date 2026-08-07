"""2.0-B3 T5 — the conversation MEDIUM ceiling, regression-guarded (AC5).

AC5: "Conversation-derived content never lifts a finding above MEDIUM on its own
(regression against the standing ceiling)."

The ceiling is not new — COR-05 has held it since R16-A2 and the R16-C1 T3 clamp
guards it a second time. What is new is that 2.0-B3 opened two routes around it that
did not exist when it was written, and this suite exists to keep both shut:

  * **the assembly route.** Since R18-A4 the retrieval substrate indexes Slack and
    Teams threads as ``conversation`` chunks, which reach a finding through
    ``context_assembly`` — a path COR-05 never sees, because COR-05 governs detector
    signals, not retrieved evidence.
  * **the configuration route.** 2.0-B3 T1 made precedence EDITABLE. A deployment can
    now reorder ``source_type_ranks`` so conversation outranks structured records.
    That is a legitimate composition choice, and it must not be able to lift a
    conversation-only finding above MEDIUM. The test that proves it
    (``test_reordered_precedence_cannot_defeat_the_ceiling``) is the single most
    load-bearing case in this file: it is the one asserting that a config edit cannot
    disable a safety rule.

The suite therefore covers the STANDING ceiling (so B3 has not regressed it), the
NEW assembly-layer ceiling, and the interaction between them.

DB-free.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone

import pytest

from app import conversation_ceiling as ceiling
from app.assembly_policy_config import parse_declared_policy
from app.context_assembly import (
    AssemblyPolicy,
    Candidate,
    KIND_ENTITY,
    KIND_EVIDENCE,
    assemble_context,
)
from app.corroboration_engine import (
    apply_corroboration_confidence,
    evaluate_corroboration,
)
from discovery.packs.corroboration_rules import (
    CONFIDENCE_HIGH,
    CONFIDENCE_LOW,
    CONFIDENCE_MEDIUM,
    CORROBORATION_RULES,
)

RUN_TS = datetime(2026, 6, 15, tzinfo=timezone.utc)
RECENT = "2026-06-10T10:00:00Z"


def chunk(cid, source_type, source_system="slack"):
    payload = {
        "chunk_id": cid,
        "content_type": source_type,
        "source_system": source_system,
        "confidence": 0.9,
    }
    return Candidate(
        candidate_id=cid,
        kind=KIND_EVIDENCE,
        origin="observed",
        confidence=0.9,
        source_type=source_type,
        payload=payload,
    )


# ---------------------------------------------------------------------------
# The standing ceiling — regression against R16-A2 / R16-C1 T3
# ---------------------------------------------------------------------------

class TestTheStandingCeilingStillHolds:
    def _conversation_only_run(self, source):
        return {
            "connected_systems": ["salesforce", source],
            source: {"escalation_pattern": {"fired": True, "timestamp": RECENT}},
        }

    @pytest.mark.parametrize("source", ["slack", "teams"])
    def test_conversation_source_alone_stays_medium(self, source):
        result = evaluate_corroboration(
            "ANY_DETECTOR", "service_cloud", self._conversation_only_run(source),
            RUN_TS, "org-a",
        )
        assert "COR-05" in result.rule_ids
        assert "COR-06" not in result.rule_ids
        assert result.elevated_confidence == CONFIDENCE_MEDIUM
        assert result.confidence_elevated is False

    @pytest.mark.parametrize("source", ["slack", "teams"])
    def test_conversation_source_never_lifts_a_scorer_baseline(self, source):
        result = evaluate_corroboration(
            "ANY_DETECTOR", "service_cloud", self._conversation_only_run(source),
            RUN_TS, "org-a",
        )
        assert apply_corroboration_confidence(CONFIDENCE_MEDIUM, result) == CONFIDENCE_MEDIUM

    def test_cor05_is_declared_non_elevating_in_the_registry(self):
        rule = CORROBORATION_RULES["COR-05"]
        assert rule.elevates is False
        assert rule.elevation_target == CONFIDENCE_MEDIUM

    def test_conversation_with_a_primary_corroborator_may_reach_high(self):
        """The ceiling is not a ban on chat — it is a ban on chat ALONE. If this case
        ever stopped reaching HIGH the ceiling would have become a suppression bug."""
        run_data = {
            "connected_systems": ["salesforce", "servicenow", "slack"],
            "servicenow": {
                "incidents": [
                    {
                        "sys_created_on": RECENT,
                        "state": "Open",
                        "team": "ops",
                        "detector_ids": ["ANY_DETECTOR"],
                    }
                ]
            },
            "slack": {"escalation_pattern": {"fired": True, "timestamp": RECENT}},
        }
        result = evaluate_corroboration(
            "ANY_DETECTOR", "service_cloud", run_data, RUN_TS, "org-a"
        )
        assert "COR-06" in result.rule_ids
        assert result.elevated_confidence == CONFIDENCE_HIGH

    def test_the_clamp_catches_a_drifted_verdict(self):
        """Defence in depth: even if a future rule wrongly set HIGH on a COR-05-only
        result, the clamp brings it back to MEDIUM."""
        result = evaluate_corroboration(
            "ANY_DETECTOR", "service_cloud", self._conversation_only_run("slack"),
            RUN_TS, "org-a",
        )
        drifted = copy.copy(result)
        object.__setattr__(drifted, "elevated_confidence", CONFIDENCE_HIGH)
        assert drifted.rule_ids == ["COR-05"]
        assert apply_corroboration_confidence(CONFIDENCE_MEDIUM, drifted) == CONFIDENCE_MEDIUM


# ---------------------------------------------------------------------------
# The assembly-layer ceiling — the route B3 opened
# ---------------------------------------------------------------------------

class TestAssemblyLayerCeiling:
    def test_conversation_only_evidence_caps_at_medium(self):
        assessment = ceiling.assess_evidence(
            [chunk("c1", "conversation"), chunk("c2", "conversation")]
        )
        assert assessment.applies
        assert assessment.ceiling == CONFIDENCE_MEDIUM
        assert assessment.conversation_evidence == 2
        assert ceiling.apply_ceiling(CONFIDENCE_HIGH, assessment) == (CONFIDENCE_MEDIUM, True)

    def test_mixed_evidence_is_not_capped(self):
        assessment = ceiling.assess_evidence(
            [chunk("c1", "conversation"), chunk("c2", "structured", "servicenow")]
        )
        assert not assessment.applies
        assert ceiling.apply_ceiling(CONFIDENCE_HIGH, assessment) == (CONFIDENCE_HIGH, False)

    def test_no_conversation_evidence_is_not_capped(self):
        assessment = ceiling.assess_evidence([chunk("c1", "prose", "confluence")])
        assert not assessment.applies

    def test_no_evidence_at_all_is_not_capped(self):
        """An absence of evidence is a different problem, handled by the confidence
        floor and the single-source rule. Capping here would misattribute it."""
        assert not ceiling.assess_evidence([]).applies

    def test_untyped_evidence_counts_as_other_not_conversation(self):
        """The ceiling fires on positive knowledge that the support is chat, never on
        a producer's silence — capping a finding for a missing metadata field would
        be the platform being arbitrary."""
        assessment = ceiling.assess_evidence(
            [chunk("c1", "conversation"), chunk("c2", "")]
        )
        assert not assessment.applies
        assert assessment.other_evidence == 1

    @pytest.mark.parametrize("label", ["conversation", "CONVERSATION", " Conversation ", "chat"])
    def test_conversation_labels_are_recognised(self, label):
        assert ceiling.is_conversation_source_type(label)

    def test_the_ceiling_caps_and_never_lowers(self):
        assessment = ceiling.assess_evidence([chunk("c1", "conversation")])
        assert ceiling.apply_ceiling(CONFIDENCE_LOW, assessment) == (CONFIDENCE_LOW, False)
        assert ceiling.apply_ceiling(CONFIDENCE_MEDIUM, assessment) == (CONFIDENCE_MEDIUM, False)

    def test_the_ceiling_never_promotes(self):
        assessment = ceiling.assess_evidence([chunk("c1", "conversation")])
        assert ceiling.apply_ceiling(CONFIDENCE_LOW, assessment)[0] == CONFIDENCE_LOW

    def test_an_unknown_confidence_is_returned_untouched(self):
        assessment = ceiling.assess_evidence([chunk("c1", "conversation")])
        assert ceiling.apply_ceiling("", assessment) == ("", False)

    def test_graph_entities_are_not_counted_as_corroborating_evidence(self):
        """A conversation-only finding must not clear the ceiling merely by naming the
        entity the thread mentions — the graph is its subject, not a second source.
        This is COR-08's no-self-corroboration rule at the assembly layer."""
        candidates = [
            Candidate(
                candidate_id="e1", kind=KIND_ENTITY, origin="observed",
                source_type="structured", payload={"entity_id": "e1"},
            ),
            chunk("c1", "conversation"),
        ]
        assert ceiling.assess_candidates(candidates).applies

    def test_assessment_reports_its_basis(self):
        assessment = ceiling.assess_evidence(
            [chunk("c1", "conversation"), chunk("c2", "conversation")]
        )
        serialised = assessment.to_dict()
        assert serialised["conversation_evidence"] == 2
        assert serialised["other_evidence"] == 0
        assert "conversation" in serialised["reason"].lower()


# ---------------------------------------------------------------------------
# Through the assembler — including the configuration route
# ---------------------------------------------------------------------------

class TestThroughTheAssembler:
    def _package(self, evidence, policy=None):
        return assemble_context(
            opportunity={"id": "opp-1"},
            graph={"entities": [], "relationships": []},
            policy=policy or AssemblyPolicy(),
            evidence_source=lambda *_a, **_k: evidence,
        )

    CONVERSATION_CHUNK = {
        "chunk_id": "c1",
        "content_type": "conversation",
        "source_system": "slack",
        "confidence": 0.95,
    }
    STRUCTURED_CHUNK = {
        "chunk_id": "c2",
        "content_type": "structured",
        "source_system": "servicenow",
        "confidence": 0.6,
    }

    def test_conversation_only_package_carries_the_ceiling(self):
        package = self._package([self.CONVERSATION_CHUNK])
        assert package.confidence_ceiling == CONFIDENCE_MEDIUM
        assert package.ceiling_assessment["applies"] is True

    def test_mixed_package_carries_no_ceiling(self):
        package = self._package([self.CONVERSATION_CHUNK, self.STRUCTURED_CHUNK])
        assert package.confidence_ceiling is None
        assert package.ceiling_assessment["applies"] is False

    def test_reordered_precedence_cannot_defeat_the_ceiling(self):
        """2.0-B3 T1 made precedence configuration. It must not be able to switch off
        a safety rule: rank conversation FIRST and the ceiling still binds, because it
        is derived from the evidence itself, not from any policy a deployment edits."""
        declaration = parse_declared_policy(
            {
                "version": 1,
                "budget_partitions": [],
                "ranking": ["source_type", "confidence", "candidate_id"],
                "origin_ranks": {"observed": 0, "inferred": 1},
                # Conversation promoted above every other source type.
                "source_type_ranks": {
                    "conversation": 0, "structured": 1, "prose": 2, "code": 3,
                },
            }
        )
        policy = AssemblyPolicy.declared(declaration)
        package = self._package([self.CONVERSATION_CHUNK], policy)
        assert package.confidence_ceiling == CONFIDENCE_MEDIUM
        assert ceiling.apply_ceiling(
            CONFIDENCE_HIGH, ceiling.assess_package(package)
        ) == (CONFIDENCE_MEDIUM, True)

    def test_a_budget_that_drops_the_only_structured_chunk_reinstates_the_ceiling(self):
        """The ceiling reflects the evidence actually composed. If the budget left only
        chat in the prompt, the finding is conversation-supported in fact, and saying
        otherwise would claim support that never reached the narrative."""
        conversation = dict(self.CONVERSATION_CHUNK, confidence=0.95)
        structured = dict(self.STRUCTURED_CHUNK, confidence=0.10)
        package = self._package(
            [conversation, structured], AssemblyPolicy(max_evidence_chunks=1)
        )
        assert [e["chunk_id"] for e in package.evidence] == ["c1"]
        assert package.confidence_ceiling == CONFIDENCE_MEDIUM

    def test_ceiling_failure_degrades_without_breaking_assembly(self, monkeypatch):
        import app.context_assembly as ca

        monkeypatch.setattr(
            ca._ceiling,
            "assess_candidates",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        package = self._package([self.CONVERSATION_CHUNK])
        assert package.evidence  # the finding kept its context
        assert package.confidence_ceiling is None


# ---------------------------------------------------------------------------
# One definition of the ceiling
# ---------------------------------------------------------------------------

class TestOneDefinition:
    def test_the_ceiling_constant_comes_from_the_corroboration_vocabulary(self):
        """One vocabulary, so no surface can hold a different idea of how high
        conversation-only evidence may reach."""
        assert ceiling.CONVERSATION_CEILING == CONFIDENCE_MEDIUM

    def test_every_enforcement_point_is_registered(self):
        points = ceiling.enforcement_points()
        assert any("COR-05" in p for p in points)
        assert any("apply_corroboration_confidence" in p for p in points)
        assert any("conversation_ceiling" in p for p in points)

    def test_the_module_does_not_redefine_the_confidence_order(self):
        import inspect

        source = inspect.getsource(ceiling)
        assert "CONFIDENCE_ORDER = " not in source
        assert 'CONFIDENCE_MEDIUM = "' not in source
