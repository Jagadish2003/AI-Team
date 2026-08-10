"""2.0-B3 T4 — narrative discipline enforcement (AC4).

AC4: "Every asserted claim in an assembled narrative resolves to supporting
evidence; a seeded unsupported claim fails the contract test."

The unit of assertion in an AgentIQ narrative is the why-bullet (``aiWhyBullets``);
each carries a ``[OBSERVED]`` / ``[INFERRED: <basis>]`` provenance tag that IS its
declared link to evidence (see ``app/narrative_discipline.py`` for the full rule
set). This suite proves three things:

  * **a well-formed narrative passes** — every claim traces back to the evidence
    the assembly composed the finding from (the :class:`ContextPackage`);
  * **a SEEDED UNSUPPORTED CLAIM FAILS** — the literal AC4 requirement, exercised
    across every way a claim can fail to trace (untagged, unverified, an observed
    claim naming an entity the package does not contain, an inference with no
    basis, an observed claim with no observed evidence behind it, and the
    forward-looking structured form citing an id outside the support set);
  * **the boundary form blocks** — ``assert_supported`` raises on the seeded
    finding, so a caller can enforce the discipline at composition time and CI
    fails when a claim does not trace.

The support set is built from a REAL ``assemble_context`` package (the same
claim→evidence linkage 2.0-B1's trace will render), so this is a contract over
the assembled narrative, not a synthetic check. DB-free: the checker and the
assembler are both pure.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.context_assembly import AssemblyPolicy, assemble_context
from app import narrative_discipline as nd
from app.narrative_discipline import (
    REASON_EVIDENCE_ID_NOT_IN_SUPPORT,
    REASON_INFERRED_NO_BASIS,
    REASON_NO_EVIDENCE_CITED,
    REASON_OBSERVED_NO_SUPPORT,
    REASON_UNTAGGED,
    REASON_UNVERIFIED,
    SupportIndex,
    UnsupportedNarrativeClaimError,
    assert_supported,
    is_supported,
    scan_narrative,
    split_provenance_tag,
)


# ---------------------------------------------------------------------------
# Builders — a real assembled package, exactly the shape the enrichment layer
# composes a narrative from.
# ---------------------------------------------------------------------------

def _graph():
    return {
        "entities": [
            {
                "entity_id": "e-payments",
                "display_name": "Payments Team",
                "source_system": "servicenow",
                "origin": "observed",
                "confidence": 0.92,
            },
            {
                "entity_id": "e-billing",
                "display_name": "Billing Service",
                "source_system": "servicenow",
                "origin": "observed",
                "confidence": 0.88,
            },
        ],
        "relationships": [],
    }


def _evidence_source(*_a, **_k):
    return [
        {"chunk_id": "chunk-1", "confidence": 0.8, "origin": "observed",
         "source_type": "prose"},
    ]


OPP = {"id": "opp-b3-t4", "evidenceIds": ["ev-77"]}


def _assemble():
    return assemble_context(
        opportunity=OPP, graph=_graph(), policy=AssemblyPolicy(),
        evidence_source=_evidence_source,
    )


def _support():
    return SupportIndex.from_context_package(_assemble(), OPP)


# ---------------------------------------------------------------------------
# A well-formed narrative passes
# ---------------------------------------------------------------------------

class TestWellFormedNarrativePasses:
    def test_the_support_is_built_from_the_assembled_package(self):
        support = _support()
        # Names, ids and observed-evidence all reach the support set.
        assert "payments team" in support.names
        assert "billing service" in support.names
        assert "chunk-1" in support.evidence_ids
        assert "ev-77" in support.evidence_ids          # the opp's own evidenceIds
        assert support.has_observed is True

    def test_every_claim_traces_back_to_evidence(self):
        support = _support()
        narrative = {
            "aiSummary": "The Payments Team is repeatedly reworking billing cases.",
            "aiWhyBullets": [
                "[OBSERVED] The Payments Team reassigned Billing Service incidents repeatedly.",
                "[INFERRED: co-firing recurrence and reassignment signals] a shared root cause is likely.",
            ],
            "aiRisks": ["Continued rework if unaddressed."],
            "aiSuggestedNextSteps": ["Review the Billing Service runbook."],
        }
        assert scan_narrative(narrative, support) == []
        # The boundary form is a no-op on a clean narrative.
        assert_supported(narrative, support, where="test")

    def test_an_empty_bullet_asserts_nothing_and_is_not_flagged(self):
        support = _support()
        narrative = {"aiWhyBullets": ["", "   "]}
        assert scan_narrative(narrative, support) == []


# ---------------------------------------------------------------------------
# AC4 core — a seeded unsupported claim FAILS
# ---------------------------------------------------------------------------

class TestSeededUnsupportedClaimFails:
    # (label, seeded bullet, expected reason). Each row is one way a claim can
    # fail to trace back to supporting evidence.
    CASES = [
        ("untagged",
         "The Payments Team is the bottleneck.",
         REASON_UNTAGGED),
        ("unverified",
         "[UNVERIFIED] Something is wrong with billing.",
         REASON_UNVERIFIED),
        ("observed_names_absent_entity",
         "[OBSERVED] Team Zeta caused the outage.",
         REASON_OBSERVED_NO_SUPPORT),
        ("inferred_without_basis",
         "[INFERRED: ] there is probably a shared cause.",
         REASON_INFERRED_NO_BASIS),
    ]

    @pytest.mark.parametrize("label,bullet,reason", CASES, ids=[c[0] for c in CASES])
    def test_seeded_unsupported_claim_is_flagged(self, label, bullet, reason):
        support = _support()
        narrative = {"aiWhyBullets": [bullet]}
        violations = scan_narrative(narrative, support)
        assert len(violations) == 1, f"{label}: expected exactly one violation"
        assert violations[0].reason == reason
        assert violations[0].field == "aiWhyBullets"
        assert violations[0].index == 0
        assert is_supported(bullet, support) is False

    @pytest.mark.parametrize("label,bullet,reason", CASES, ids=[c[0] for c in CASES])
    def test_boundary_form_raises_on_the_seeded_claim(self, label, bullet, reason):
        support = _support()
        narrative = {"aiWhyBullets": [bullet]}
        with pytest.raises(UnsupportedNarrativeClaimError) as exc:
            assert_supported(narrative, support, where=f"seeded:{label}")
        # The failure names the finding and every offending claim (auditable).
        assert f"seeded:{label}" in str(exc.value)
        assert bullet in str(exc.value)
        assert exc.value.violations[0].reason == reason

    def test_observed_claim_with_no_observed_evidence_is_unsupported(self):
        # A support set with names but NO observed evidence — an observed claim
        # has nothing to stand on even if it references a known name.
        support = SupportIndex.from_parts(
            names=["Payments Team"], evidence_ids=[], has_observed=False,
        )
        bullet = "[OBSERVED] The Payments Team reassigned incidents repeatedly."
        violations = scan_narrative({"aiWhyBullets": [bullet]}, support)
        assert len(violations) == 1
        assert violations[0].reason == REASON_OBSERVED_NO_SUPPORT

    def test_one_bad_claim_among_good_ones_is_isolated(self):
        support = _support()
        narrative = {
            "aiWhyBullets": [
                "[OBSERVED] The Payments Team reassigned Billing Service incidents.",
                "[OBSERVED] Team Zeta was also involved.",            # unsupported
                "[INFERRED: recurrence signal] a shared cause is likely.",
            ]
        }
        violations = scan_narrative(narrative, support)
        assert len(violations) == 1
        assert violations[0].index == 1


# ---------------------------------------------------------------------------
# Through the assembler — end to end over a real ContextPackage
# ---------------------------------------------------------------------------

class TestThroughTheAssembler:
    def test_a_hallucinated_entity_fails_against_the_assembled_support(self):
        package = _assemble()
        support = SupportIndex.from_context_package(package, OPP)
        # A finding whose narrative names an entity the assembly never composed.
        narrative = {"aiWhyBullets": ["[OBSERVED] The Logistics Team owns this queue."]}
        with pytest.raises(UnsupportedNarrativeClaimError):
            assert_supported(narrative, support, where="assembled-finding")

    def test_a_claim_naming_a_selected_entity_passes(self):
        package = _assemble()
        support = SupportIndex.from_context_package(package, OPP)
        narrative = {"aiWhyBullets": ["[OBSERVED] The Billing Service shows recurring rework."]}
        assert scan_narrative(narrative, support) == []

    def test_a_budget_trimmed_entity_is_not_valid_support(self):
        """A claim can only trace to what actually survived into the package —
        an entity dropped by budget is no longer supporting evidence."""
        package = assemble_context(
            opportunity=OPP, graph=_graph(),
            policy=AssemblyPolicy(max_entities=1), evidence_source=_evidence_source,
        )
        support = SupportIndex.from_context_package(package, OPP)
        # 'Payments Team' ranks first and survives; 'Billing Service' is trimmed.
        kept = "[OBSERVED] The Payments Team reassigned incidents."
        dropped = "[OBSERVED] The Billing Service is the bottleneck."
        assert scan_narrative({"aiWhyBullets": [kept]}, support) == []
        assert len(scan_narrative({"aiWhyBullets": [dropped]}, support)) == 1


# ---------------------------------------------------------------------------
# The forward-looking structured claim form (what 2.0-B1's trace will carry)
# ---------------------------------------------------------------------------

class TestStructuredClaimForm:
    def test_structured_claim_citing_a_support_id_passes(self):
        support = _support()
        claim = {"text": "Rework recurs on billing.", "evidenceIds": ["chunk-1"]}
        assert scan_narrative({"aiWhyBullets": [claim]}, support) == []

    def test_structured_claim_citing_an_absent_id_fails(self):
        support = _support()
        claim = {"text": "Rework recurs on billing.", "evidenceIds": ["chunk-999"]}
        violations = scan_narrative({"aiWhyBullets": [claim]}, support)
        assert len(violations) == 1
        assert violations[0].reason == REASON_EVIDENCE_ID_NOT_IN_SUPPORT

    def test_structured_claim_citing_nothing_fails(self):
        support = _support()
        claim = {"text": "Rework recurs on billing.", "evidenceIds": []}
        violations = scan_narrative({"aiWhyBullets": [claim]}, support)
        assert len(violations) == 1
        assert violations[0].reason == REASON_NO_EVIDENCE_CITED


# ---------------------------------------------------------------------------
# Scope — only asserted-claim fields are held to account
# ---------------------------------------------------------------------------

class TestScope:
    def test_risks_and_next_steps_are_not_scanned(self):
        """Risks are conditionals and next-steps are recommendations, not
        assertions of current fact — flagging them would train authors to
        ignore the guard."""
        support = _support()
        narrative = {
            "aiWhyBullets": ["[OBSERVED] The Payments Team reassigned incidents."],
            "aiRisks": ["Team Zeta may be affected."],                 # unsupported entity, but a RISK
            "aiSuggestedNextSteps": ["Escalate to the Logistics Team."],  # unsupported entity, but an ACTION
        }
        assert scan_narrative(narrative, support) == []

    def test_ai_summary_is_not_a_claim_field(self):
        support = _support()
        narrative = {
            "aiSummary": "Team Zeta is the root cause.",  # prose synthesis, not a discrete claim
            "aiWhyBullets": [],
        }
        assert scan_narrative(narrative, support) == []

    def test_claim_fields_are_exactly_the_why_bullets(self):
        assert nd.NARRATIVE_CLAIM_FIELDS == ("aiWhyBullets",)


# ---------------------------------------------------------------------------
# Tag parsing
# ---------------------------------------------------------------------------

class TestTagParsing:
    @pytest.mark.parametrize("text,tag,basis", [
        ("[OBSERVED] x", "OBSERVED", None),
        ("[INFERRED: co-firing] x", "INFERRED", "co-firing"),
        ("[INFERRED] x", "INFERRED", ""),
        ("[INFERRED: ] x", "INFERRED", ""),
        ("[UNVERIFIED] x", "UNVERIFIED", None),
        ("no tag here", "", None),
        ("[observed] lowercase", "OBSERVED", None),
    ])
    def test_split(self, text, tag, basis):
        got_tag, got_basis, body = split_provenance_tag(text)
        assert got_tag == tag
        assert got_basis == basis
        assert "x" in body or "lowercase" in body or body == "no tag here"


# ---------------------------------------------------------------------------
# The checker is PURE — the property that lets it run identically in a test and
# at a serve boundary (mirrors discovery/projection/vocabulary.py's discipline).
# ---------------------------------------------------------------------------

class TestPurity:
    FORBIDDEN = ("psycopg2", "sqlalchemy", "app.db", "llm_enrichment",
                 "anthropic", "requests", "httpx", "datetime", "time", "random")

    def test_module_imports_nothing_stateful(self):
        src = Path(nd.__file__).read_text(encoding="utf-8")
        tree = ast.parse(src)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
        for bad in self.FORBIDDEN:
            assert not any(bad in m for m in imported), (
                f"narrative_discipline must stay pure; found forbidden import {bad!r}"
            )
