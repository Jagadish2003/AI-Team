"""2.0-B3 T7 (AT-809) — acceptance-criteria validation for the whole story.

This is the STORY-LEVEL sign-off gate for 2.0-B3 (Context Assembly Maturity). The
six sub-tasks each ship a focused suite that drills into one module:

    AC1  T1  tests/unit/test_r2_0_b3_t1_assembly_policy.py
    AC2  T2  tests/unit/test_r2_0_b3_t2_budgeted_composition.py
    AC3  T3  tests/unit/test_r2_0_b3_t3_contradiction_handling.py
    AC4  T4  tests/contract/test_r2_0_b3_t4_narrative_discipline.py
    AC5  T5  tests/unit/test_r2_0_b3_t5_conversation_ceiling.py
    AC6  T6  tests/contract/test_r2_0_b3_t6_mode_parity.py

Those suites prove each behaviour deeply. THIS suite proves the six acceptance
criteria hold *together*, exercised through the public surfaces the rest of the
platform actually consumes — one `assemble_context` package, one `build_graph_context`
finding, one mode report — so a reviewer can walk the whole story's AC table against a
single file. It mirrors the R16-B2 pattern (`test_context_assembly_acceptance.py`),
which does the same for the assembly foundation this story matured.

Each AC class asserts BOTH sides of its criterion: the property holds, AND the
counterfactual that would signal a regression is demonstrably different — an
acceptance test that only shows the happy path can pass while the guarantee is
hollow.

DB-free: `assemble_context` is pure, and `build_graph_context` is driven with
`org_id=None` so no store is touched.
"""
from __future__ import annotations

import ast
import copy
import json
from pathlib import Path

import pytest

from app import assembly_policy_config as apc
from app import conversation_ceiling as ceiling
from app import context_contradictions as cc
from app import mode_parity as mp
from app.context_assembly import (
    DECISION_EXCLUDED,
    DECISION_INCLUDED,
    AssemblyPolicy,
    assemble_context,
)
from app.graph_context import build_graph_context
from app.mode_parity import (
    ALL_MODES,
    RETRIEVAL_UNAVAILABLE_LABEL,
    STEP_RETRIEVAL,
    active_embedding_mode,
    assembly_mode_report,
)
from app.narrative_discipline import (
    SupportIndex,
    UnsupportedNarrativeClaimError,
    assert_supported,
    scan_narrative,
)
from discovery.packs.corroboration_rules import CONFIDENCE_HIGH, CONFIDENCE_MEDIUM

_GEN_ENV = "MODEL_GENERATION_PROVIDER"
_EMB_ENV = "MODEL_EMBEDDING_PROVIDER"

OPP = {"id": "opp-b3-t7", "evidenceIds": ["ev-77"]}


# --------------------------------------------------------------------------- #
# Shared builders — one coherent estate the whole gate composes from.
# --------------------------------------------------------------------------- #

def _raw_declaration() -> dict:
    return json.loads(Path(apc.DEFAULT_CONFIG_PATH).read_text(encoding="utf-8"))


def _declared(**changes) -> apc.DeclaredAssemblyPolicy:
    """The shipped declaration with the given top-level keys replaced."""
    raw = copy.deepcopy(_raw_declaration())
    raw.update(changes)
    return apc.parse_declared_policy(raw)


def _shipped_policy(**overrides) -> AssemblyPolicy:
    return AssemblyPolicy.declared(apc.load_declared_policy(), **overrides)


def _contradiction_graph() -> dict:
    """Two sources naming a DIFFERENT owner for one subject — the AC3 disagreement,
    and enough entities/relationships that AC2's budget can bind."""
    return {
        "entities": [
            {"entity_id": "e-cmdb", "display_name": "Payments API",
             "source_system": "servicenow", "origin": "observed",
             "confidence": 0.92, "metadata": {"owner": "Platform Engineering"}},
            {"entity_id": "e-runbook", "display_name": "Payments API",
             "source_system": "confluence", "origin": "observed",
             "confidence": 0.80, "metadata": {"owner": "L2 Support"}},
            {"entity_id": "e-billing", "display_name": "Billing Service",
             "source_system": "servicenow", "origin": "observed", "confidence": 0.75},
        ],
        "relationships": [
            {"from_id": "e-cmdb", "to_id": "e-billing", "relationship_type": "owns",
             "confidence": 0.7, "inferred": False},
        ],
    }


def _evidence_source(*_a, **_k):
    return [
        {"chunk_id": "chunk-1", "origin": "observed", "confidence": 0.8,
         "source_type": "prose", "source_system": "confluence"},
    ]


def _assemble(policy=None, graph=None, evidence_source=_evidence_source):
    return assemble_context(
        opportunity=OPP,
        graph=graph if graph is not None else _contradiction_graph(),
        policy=policy or AssemblyPolicy(),
        evidence_source=evidence_source,
    )


def _selected_evidence_ids(package):
    return [e.get("chunk_id") for e in package.evidence]


# ════════════════════════════════════════════════════════════════════════════
# AC1 — assembly policy is declared configuration; changing precedence changes
#       composition without code changes.
# ════════════════════════════════════════════════════════════════════════════

class TestAC1DeclaredPrecedence:
    # Two evidence chunks that DISAGREE across dimensions on purpose: the chat is
    # more confident, the record is a better source type. Precedence — not the
    # confidence number — decides which one survives a budget of 1.
    _EVIDENCE = [
        {"chunk_id": "chat", "origin": "observed", "confidence": 0.95,
         "source_type": "conversation", "source_system": "slack"},
        {"chunk_id": "rec", "origin": "observed", "confidence": 0.60,
         "source_type": "structured", "source_system": "servicenow"},
    ]

    def _order_under(self, ranking):
        policy = AssemblyPolicy.declared(
            _declared(ranking=ranking), max_evidence_chunks=2,
        )
        pkg = assemble_context(OPP, {}, policy, evidence_source=list(self._EVIDENCE))
        return _selected_evidence_ids(pkg)

    def _survivor_under(self, ranking):
        policy = AssemblyPolicy.declared(
            _declared(ranking=ranking), max_evidence_chunks=1,
        )
        pkg = assemble_context(OPP, {}, policy, evidence_source=list(self._EVIDENCE))
        return _selected_evidence_ids(pkg)

    def test_reordering_the_declaration_changes_composition(self):
        """The load-bearing AC1 assertion, through the public assembler: same code,
        same inputs, a different declaration → a different composed order."""
        source_type_first = self._order_under(
            ["source_type", "confidence", "freshness", "candidate_id"])
        confidence_first = self._order_under(
            ["confidence", "source_type", "freshness", "candidate_id"])

        assert source_type_first == ["rec", "chat"]
        assert confidence_first == ["chat", "rec"]
        assert source_type_first != confidence_first

    def test_precedence_changes_the_selected_set_not_only_the_order(self):
        """Under a binding budget, precedence decides who gets into the finding at
        all — otherwise AC1 would be cosmetic (identical composition, reshuffled)."""
        assert self._survivor_under(
            ["source_type", "confidence", "freshness", "candidate_id"]) == ["rec"]
        assert self._survivor_under(
            ["confidence", "source_type", "freshness", "candidate_id"]) == ["chat"]

    def test_the_shipped_declaration_is_what_runs(self):
        """The precedence a deployment runs comes from the file, not from constants —
        so an operator edit takes effect with no code change."""
        declaration = apc.load_declared_policy()
        assert declaration.ranking[0] == apc.DIMENSION_SOURCE_TYPE
        assert declaration.ranking[-1] == apc.DIMENSION_CANDIDATE_ID
        # And the shipped ordering really prefers the structured record over chat.
        assert self._survivor_under(list(declaration.ranking)) == ["rec"]

    def test_the_package_records_the_declaration_that_produced_it(self):
        """A selection_log read later must be interpretable against the precedence in
        force when it was written — now editable, so the log alone no longer explains
        itself."""
        pkg = _assemble(policy=_shipped_policy())
        assert pkg.policy_declaration is not None
        assert pkg.policy_declaration["ranking"][0] == apc.DIMENSION_SOURCE_TYPE

    def test_a_typo_in_the_declaration_fails_loudly(self):
        """A silently-dropped precedence rule would change composition with nobody
        aware. The loader refuses the whole class of that mistake."""
        with pytest.raises(apc.AssemblyPolicyConfigError):
            _declared(ranking=["confidenec", "candidate_id"])          # unknown dim
        with pytest.raises(apc.AssemblyPolicyConfigError):
            _declared(ranking=["confidence", "freshness"])             # no tiebreaker


# ════════════════════════════════════════════════════════════════════════════
# AC2 — over-budget candidate sets select deterministically and record what was
#       dropped and why.
# ════════════════════════════════════════════════════════════════════════════

class TestAC2BudgetedComposition:
    def _over_budget(self):
        graph = {
            "entities": [
                {"entity_id": f"e{i:02d}", "confidence": 0.9 - i / 100} for i in range(20)
            ],
            "relationships": [
                {"relationship_id": f"r{i:02d}", "confidence": 0.9 - i / 100,
                 "inferred": False} for i in range(25)
            ],
        }
        # A hard total-item budget well below the offered count forces trimming.
        raw = copy.deepcopy(_raw_declaration())
        raw["caps"]["total_items"] = 12
        policy = AssemblyPolicy.declared(apc.parse_declared_policy(raw))
        return assemble_context({"id": "o1"}, graph, policy)

    def test_over_budget_selection_is_deterministic(self):
        """AC2 first half — including the audit trail. A stable selection with a
        shifting log or report is not reproducible."""
        first = self._over_budget()
        second = self._over_budget()
        assert [e["entity_id"] for e in first.entities] == [
            e["entity_id"] for e in second.entities
        ]
        assert first.selection_log == second.selection_log
        assert first.budget_report == second.budget_report

    def test_what_was_dropped_is_recorded_with_a_reason(self):
        """AC2 second half — every trimmed candidate is on the log with a reason that
        names WHICH lever to pull, and the derived report reconciles with it."""
        pkg = self._over_budget()
        excluded = [e for e in pkg.selection_log if e["decision"] == DECISION_EXCLUDED]
        assert excluded, "the graph is deliberately over budget"
        for entry in excluded:
            assert entry["candidate_id"] and entry["kind"] and entry["reason"]

        report = pkg.budget_report
        assert report is not None
        # The report is DERIVED from the log, so the two cannot disagree.
        log_excluded = sum(
            1 for e in pkg.selection_log if e["decision"] == DECISION_EXCLUDED)
        log_included = sum(
            1 for e in pkg.selection_log if e["decision"] == DECISION_INCLUDED)
        assert report["total_dropped"] == log_excluded
        assert report["total_selected"] == log_included
        # And per kind the arithmetic adds up — an early version double-counted.
        for kind in report["per_kind"]:
            assert kind["offered"] == kind["selected"] + kind["dropped"]

    def test_the_report_is_json_serialisable_for_the_run_record(self):
        pkg = self._over_budget()
        assert json.loads(json.dumps(pkg.budget_report)) == pkg.budget_report

    def test_a_within_budget_finding_reports_no_breach(self):
        """The counterfactual: with room to spare, nothing is a budget drop."""
        pkg = assemble_context(
            {"id": "o1"},
            {"entities": [{"entity_id": "e1", "confidence": 0.9}], "relationships": []},
            _shipped_policy(),
        )
        assert pkg.budget_report["breached"] is False
        assert pkg.budget_report["reason"] is None
        assert pkg.budget_report["total_dropped"] == 0


# ════════════════════════════════════════════════════════════════════════════
# AC3 — seeded contradictory sources produce a finding that NAMES the
#       disagreement rather than silently resolving it.
# ════════════════════════════════════════════════════════════════════════════

class TestAC3ContradictionSurfacing:
    def test_seeded_contradiction_is_named_not_resolved(self):
        pkg = _assemble(policy=_shipped_policy())
        assert len(pkg.contradictions) == 1
        found = pkg.contradictions[0]
        assert found["attribute"] == "owner"
        # Both values and both sources survive into the copy — neither side is dropped.
        assert "Platform Engineering" in found["summary"]
        assert "L2 Support" in found["summary"]
        assert pkg.contradiction_report["detected"] == 1

    def test_both_sides_still_reach_the_finding(self):
        """Naming the disagreement must not cost either side its place in context."""
        pkg = _assemble(policy=_shipped_policy())
        owners = {
            e.get("metadata", {}).get("owner")
            for e in pkg.entities if e.get("display_name") == "Payments API"
        }
        assert owners == {"Platform Engineering", "L2 Support"}

    def test_the_finding_does_not_pick_a_winner(self):
        """The record carries no severity/score/preferred side, and the rendered copy
        contains no resolution language — the platform states the disagreement, the
        model is told not to settle it."""
        pkg = _assemble(policy=_shipped_policy())
        found = pkg.contradictions[0]
        for forbidden in ("winner", "preferred", "resolved_value", "severity", "score"):
            assert forbidden not in found
        assert cc.resolution_language_in(found["summary"]) == []

    def test_agreeing_sources_report_nothing(self):
        """The counterfactual: aligned owners are not a contradiction."""
        graph = _contradiction_graph()
        graph["entities"][1]["metadata"]["owner"] = "Platform Engineering"
        pkg = assemble_context(OPP, graph, _shipped_policy(),
                               evidence_source=_evidence_source)
        assert pkg.contradictions == []
        assert pkg.contradiction_report["detected"] == 0

    def test_a_budget_that_drops_one_side_still_names_the_disagreement(self):
        """Detection runs over the ELIGIBLE candidates, so a trim cannot quietly
        resolve the argument — the story's failure mode, one layer down."""
        pkg = assemble_context(OPP, _contradiction_graph(),
                               AssemblyPolicy(max_entities=1),
                               evidence_source=_evidence_source)
        found = pkg.contradictions[0]
        assert found["fully_in_context"] is False
        assert {p["in_context"] for p in found["positions"]} == {True, False}


# ════════════════════════════════════════════════════════════════════════════
# AC4 — every asserted claim in an assembled narrative resolves to supporting
#       evidence; a seeded unsupported claim fails the contract test.
# ════════════════════════════════════════════════════════════════════════════

class TestAC4NarrativeDiscipline:
    def _support(self):
        return SupportIndex.from_context_package(_assemble(), OPP)

    def test_a_well_formed_narrative_passes(self):
        support = self._support()
        narrative = {
            "aiWhyBullets": [
                "[OBSERVED] The Payments API owner is disputed across sources.",
                "[INFERRED: co-firing recurrence and reassignment signals] a shared root cause is likely.",
            ],
        }
        assert scan_narrative(narrative, support) == []
        assert_supported(narrative, support, where="t7-ac4")  # boundary form: no-op

    def test_a_seeded_unsupported_claim_fails_the_contract_test(self):
        """The literal AC4 requirement. A claim that names an entity the assembly
        never composed does not trace back to evidence — the boundary form raises and
        CI fails."""
        support = self._support()
        narrative = {"aiWhyBullets": ["[OBSERVED] Team Zeta caused the outage."]}

        violations = scan_narrative(narrative, support)
        assert len(violations) == 1
        with pytest.raises(UnsupportedNarrativeClaimError) as exc:
            assert_supported(narrative, support, where="seeded-unsupported")
        assert "seeded-unsupported" in str(exc.value)

    @pytest.mark.parametrize("bullet", [
        "The Payments API is the bottleneck.",                 # untagged
        "[UNVERIFIED] Something is wrong with billing.",       # unverified
        "[INFERRED: ] there is probably a shared cause.",      # inference, no basis
    ])
    def test_each_shape_of_unsupported_claim_is_flagged(self, bullet):
        support = self._support()
        assert len(scan_narrative({"aiWhyBullets": [bullet]}, support)) == 1

    def test_one_bad_claim_among_good_ones_is_isolated(self):
        support = self._support()
        narrative = {
            "aiWhyBullets": [
                "[OBSERVED] The Payments API owner is disputed across sources.",
                "[OBSERVED] Team Zeta was also involved.",     # unsupported
            ]
        }
        violations = scan_narrative(narrative, support)
        assert len(violations) == 1
        assert violations[0].index == 1


# ════════════════════════════════════════════════════════════════════════════
# AC5 — conversation-derived content never lifts a finding above MEDIUM on its
#       own (regression against the standing ceiling).
# ════════════════════════════════════════════════════════════════════════════

class TestAC5ConversationCeiling:
    _CONVERSATION = {"chunk_id": "c1", "content_type": "conversation",
                     "source_system": "slack", "confidence": 0.95}
    _STRUCTURED = {"chunk_id": "c2", "content_type": "structured",
                   "source_system": "servicenow", "confidence": 0.60}

    def _package(self, evidence, policy=None):
        return assemble_context(
            opportunity={"id": "opp-1"},
            graph={"entities": [], "relationships": []},
            policy=policy or AssemblyPolicy(),
            evidence_source=lambda *_a, **_k: evidence,
        )

    def test_conversation_only_finding_is_capped_at_medium(self):
        pkg = self._package([self._CONVERSATION])
        assert pkg.confidence_ceiling == CONFIDENCE_MEDIUM
        assert pkg.ceiling_assessment["applies"] is True
        assert ceiling.apply_ceiling(
            CONFIDENCE_HIGH, ceiling.assess_package(pkg)) == (CONFIDENCE_MEDIUM, True)

    def test_mixed_evidence_is_not_capped(self):
        """The counterfactual: chat WITH another source type is not capped — the
        ceiling bans chat alone, not chat."""
        pkg = self._package([self._CONVERSATION, self._STRUCTURED])
        assert pkg.confidence_ceiling is None
        assert pkg.ceiling_assessment["applies"] is False

    def test_reordered_precedence_cannot_defeat_the_ceiling(self):
        """The single most load-bearing case in AC5: T1 made precedence editable, so
        a deployment could rank conversation first. The ceiling still binds because it
        is derived from the evidence itself, not from any policy a deployment edits."""
        declaration = apc.parse_declared_policy({
            "version": 1,
            "budget_partitions": [],
            "ranking": ["source_type", "confidence", "candidate_id"],
            "origin_ranks": {"observed": 0, "inferred": 1},
            "source_type_ranks": {"conversation": 0, "structured": 1,
                                   "prose": 2, "code": 3},
        })
        pkg = self._package([self._CONVERSATION], AssemblyPolicy.declared(declaration))
        assert pkg.confidence_ceiling == CONFIDENCE_MEDIUM

    def test_graph_entities_do_not_clear_the_ceiling(self):
        """A conversation-only finding must not escape the ceiling merely by naming
        the entity the thread mentions — the graph is its subject, not a second
        source (COR-08 at the assembly layer)."""
        pkg = assemble_context(
            opportunity={"id": "opp-1"},
            graph={"entities": [{"entity_id": "e1", "confidence": 0.9,
                                 "display_name": "Billing"}], "relationships": []},
            policy=AssemblyPolicy(),
            evidence_source=lambda *_a, **_k: [self._CONVERSATION],
        )
        assert pkg.confidence_ceiling == CONFIDENCE_MEDIUM


# ════════════════════════════════════════════════════════════════════════════
# AC6 — the same seeded inputs produce equivalent assembly decisions across all
#       three AI modes; unsupported steps degrade with a visible label.
# ════════════════════════════════════════════════════════════════════════════

class TestAC6ModeParity:
    def _fingerprint(self, pkg):
        return {
            "entities": [e.get("entity_id") for e in pkg.entities],
            "relationships": [r.get("relationship_id") or
                              (r.get("from_id"), r.get("to_id")) for r in pkg.relationships],
            "evidence": _selected_evidence_ids(pkg),
            "selection_log": pkg.selection_log,
            "budget_report": pkg.budget_report,
            "contradictions": pkg.contradictions,
            "confidence_ceiling": pkg.confidence_ceiling,
        }

    def test_assembly_decisions_are_identical_across_all_three_modes(self, monkeypatch):
        fingerprints = {}
        for mode in ALL_MODES:
            monkeypatch.setenv(_GEN_ENV, mode)
            monkeypatch.setenv(_EMB_ENV, mode)
            assert active_embedding_mode() == mode  # not a no-op
            fingerprints[mode] = self._fingerprint(_assemble(policy=_shipped_policy()))

        reference = fingerprints[ALL_MODES[0]]
        # Guard against a vacuous parity check over empty packages.
        assert reference["entities"] and reference["evidence"]
        for mode in ALL_MODES[1:]:
            assert fingerprints[mode] == reference, (
                f"assembly decisions differ between {ALL_MODES[0]} and {mode}")

    def test_assembly_never_touches_a_model(self):
        """The structural reason parity holds: assembly composes no prompt and calls
        no gateway, so the AI mode cannot reach an assembly decision."""
        import app.context_assembly as ca

        tree = ast.parse(Path(ca.__file__).read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
        for bad in ("model_gateway", "anthropic", "llm_enrichment"):
            assert not any(bad in m for m in imported)

    def test_hosted_embedding_step_degrades_with_a_visible_label(self, monkeypatch):
        """The one step a mode STRUCTURALLY can't support is evidence retrieval (the
        hosted provider has no embeddings endpoint). It degrades with a specific,
        visible label on the finding — never silently."""
        monkeypatch.setenv(_EMB_ENV, "hosted")
        report = assembly_mode_report(generation_mode="hosted", embedding_mode="hosted")
        assert report.degraded is True
        assert STEP_RETRIEVAL in [d.step for d in report.degradations]
        assert report.degradations[0].label == RETRIEVAL_UNAVAILABLE_LABEL
        assert RETRIEVAL_UNAVAILABLE_LABEL.strip()

        # And it reaches the composed finding, not just an out-of-band report.
        gc = build_graph_context(
            None, "run-t7",
            entities=[{"entity_id": "e-1", "display_name": "Payments API",
                       "resolution_status": "resolved"}],
            relationships=[],
        )
        assert STEP_RETRIEVAL in [d["step"] for d in gc.mode_degradations]
        assert RETRIEVAL_UNAVAILABLE_LABEL in [d["label"] for d in gc.mode_degradations]

    @pytest.mark.parametrize("embedding_mode", ["in_boundary", "customer_tenant"])
    def test_an_embedding_capable_mode_has_no_degradation(self, monkeypatch, embedding_mode):
        """The counterfactual: a mode that CAN embed carries no label, and its finding
        is byte-identical to the pre-T6 shape (empty degradation list)."""
        monkeypatch.setenv(_EMB_ENV, embedding_mode)
        gc = build_graph_context(
            None, "run-t7",
            entities=[{"entity_id": "e-1", "display_name": "Payments API",
                       "resolution_status": "resolved"}],
            relationships=[],
        )
        assert gc.mode_degradations == []


# ════════════════════════════════════════════════════════════════════════════
# Story coverage — the gate is self-describing: every AC maps to a shipped module
# and a focused per-task suite, and all six load. A missing module would fail the
# whole story, so this makes the mapping an assertion rather than a comment.
# ════════════════════════════════════════════════════════════════════════════

class TestStoryCoverage:
    _TESTS_ROOT = Path(__file__).resolve().parents[1]  # backend/tests
    _AC_SUITES = {
        "AC1": _TESTS_ROOT / "unit" / "test_r2_0_b3_t1_assembly_policy.py",
        "AC2": _TESTS_ROOT / "unit" / "test_r2_0_b3_t2_budgeted_composition.py",
        "AC3": _TESTS_ROOT / "unit" / "test_r2_0_b3_t3_contradiction_handling.py",
        "AC4": _TESTS_ROOT / "contract" / "test_r2_0_b3_t4_narrative_discipline.py",
        "AC5": _TESTS_ROOT / "unit" / "test_r2_0_b3_t5_conversation_ceiling.py",
        "AC6": _TESTS_ROOT / "contract" / "test_r2_0_b3_t6_mode_parity.py",
    }

    def test_every_ac_has_a_focused_suite_on_disk(self):
        missing = {ac: str(p) for ac, p in self._AC_SUITES.items() if not p.exists()}
        assert not missing, f"missing per-AC suites: {missing}"

    def test_all_six_maturity_modules_import(self):
        # A smoke assertion that the surfaces this gate drives are all present.
        assert apc.load_declared_policy() is not None            # AC1
        assert hasattr(assemble_context, "__call__")             # AC2
        assert hasattr(cc, "detect_contradictions")              # AC3
        assert hasattr(SupportIndex, "from_context_package")     # AC4
        assert ceiling.CONVERSATION_CEILING == CONFIDENCE_MEDIUM  # AC5
        assert set(mp.ALL_MODES) == {"hosted", "in_boundary", "customer_tenant"}  # AC6
