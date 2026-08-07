"""2.0-B3 T6 — mode parity validation (AC6).

AC6: "The same seeded inputs produce equivalent assembly decisions across all
three AI modes; unsupported steps degrade with a visible label."

Two properties, tested separately:

  * **Parity** — the same seeded `(graph, policy, evidence)` produces a
    byte-identical `ContextPackage` under hosted / in-boundary / customer-tenant,
    because `assemble_context` never calls a model (proven structurally too: it
    imports no gateway). The AI mode cannot change an assembly decision.
  * **Visible degradation** — the one step a mode can STRUCTURALLY fail to
    support is evidence retrieval (the hosted provider has no embeddings
    endpoint). In that mode assembly does not silently drop evidence: it carries
    a specific, visible label (`app/mode_parity.py`), surfaced on the composed
    finding via `GraphContext.mode_degradations`.

DB-free: `assemble_context` is pure, and `build_graph_context` is driven with
explicit entities and `org_id=None` so no store is touched.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.context_assembly import AssemblyPolicy, assemble_context
from app.graph_context import build_graph_context
from app import mode_parity as mp
from app.mode_parity import (
    ALL_MODES,
    RETRIEVAL_UNAVAILABLE_LABEL,
    STEP_RETRIEVAL,
    active_embedding_mode,
    active_generation_mode,
    assembly_mode_report,
    mode_degradations,
    retrieval_supported,
)

_GEN_ENV = "MODEL_GENERATION_PROVIDER"
_EMB_ENV = "MODEL_EMBEDDING_PROVIDER"


# ---------------------------------------------------------------------------
# Builders — one seeded input set, reused under every mode.
# ---------------------------------------------------------------------------

def _graph():
    return {
        "entities": [
            {"entity_id": "e-1", "display_name": "Payments Team",
             "origin": "observed", "confidence": 0.92, "resolution_status": "resolved"},
            {"entity_id": "e-2", "display_name": "Billing Service",
             "origin": "observed", "confidence": 0.80, "resolution_status": "resolved"},
            {"entity_id": "e-3", "display_name": "Guessed Link",
             "origin": "inferred", "confidence": 0.40, "resolution_status": "resolved"},
        ],
        "relationships": [
            {"from_id": "e-1", "to_id": "e-2", "relationship_type": "owns",
             "confidence": 0.9, "inferred": False},
        ],
    }


def _evidence_source(*_a, **_k):
    # A FIXED candidate set — the same evidence is offered in every mode, so any
    # difference in the package would have to come from assembly, not the input.
    return [
        {"chunk_id": "chunk-1", "origin": "observed", "confidence": 0.7,
         "source_type": "prose"},
        {"chunk_id": "chunk-2", "origin": "observed", "confidence": 0.3,
         "source_type": "prose"},
    ]


OPP = {"id": "opp-b3-t6"}


def _assemble():
    return assemble_context(
        opportunity=OPP, graph=_graph(), policy=AssemblyPolicy(),
        evidence_source=_evidence_source,
    )


def _ids(items, keys=("entity_id", "chunk_id", "relationship_id", "candidate_id", "id")):
    out = []
    for it in items:
        for k in keys:
            v = it.get(k) if isinstance(it, dict) else getattr(it, k, None)
            if v:
                out.append(str(v))
                break
    return out


def _fingerprint(pkg):
    """A comparable snapshot of every assembly DECISION in a package."""
    return {
        "entities": _ids(pkg.entities),
        "relationships": _ids(pkg.relationships),
        "evidence": _ids(pkg.evidence),
        "selection_log": pkg.selection_log,
        "budget_report": pkg.budget_report,
        "contradictions": pkg.contradictions,
        "confidence_ceiling": pkg.confidence_ceiling,
        "ceiling_assessment": pkg.ceiling_assessment,
    }


# ---------------------------------------------------------------------------
# Parity — the same seeded inputs produce equivalent assembly decisions
# ---------------------------------------------------------------------------

class TestAssemblyParityAcrossModes:
    def test_same_inputs_produce_identical_package_in_every_mode(self, monkeypatch):
        fingerprints = {}
        for mode in ALL_MODES:
            monkeypatch.setenv(_GEN_ENV, mode)
            monkeypatch.setenv(_EMB_ENV, mode)
            # Sanity: the active mode really is what we set (so this is not a no-op).
            assert active_generation_mode() == mode
            assert active_embedding_mode() == mode
            fingerprints[mode] = _fingerprint(_assemble())

        reference = fingerprints[ALL_MODES[0]]
        for mode in ALL_MODES[1:]:
            assert fingerprints[mode] == reference, (
                f"assembly decisions differ between {ALL_MODES[0]} and {mode}"
            )

    def test_the_package_actually_selected_something(self, monkeypatch):
        # Guards against a vacuous parity test (all-empty packages are trivially equal).
        monkeypatch.setenv(_GEN_ENV, "hosted")
        pkg = _assemble()
        assert _ids(pkg.entities), "expected the seeded entities to be selected"
        assert _ids(pkg.evidence), "expected the seeded evidence to be selected"

    def test_assembly_imports_no_model_gateway(self):
        """The structural reason parity holds: assembly never touches a model."""
        import app.context_assembly as ca

        src = Path(ca.__file__).read_text(encoding="utf-8")
        tree = ast.parse(src)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
        for bad in ("model_gateway", "anthropic", "llm_enrichment"):
            assert not any(bad in m for m in imported), (
                f"context_assembly must stay model-free; found import {bad!r}"
            )


# ---------------------------------------------------------------------------
# Visible degradation — a mode that can't support a step says so
# ---------------------------------------------------------------------------

class TestVisibleDegradationLabel:
    @pytest.mark.parametrize("embedding_mode", ALL_MODES)
    def test_retrieval_degrades_only_in_hosted_embedding_mode(self, embedding_mode):
        report = assembly_mode_report(generation_mode="hosted", embedding_mode=embedding_mode)
        if embedding_mode == "hosted":
            assert report.retrieval_supported is False
            assert report.degraded is True
            steps = [d.step for d in report.degradations]
            assert STEP_RETRIEVAL in steps
            label = report.degradations[0].label
            assert label == RETRIEVAL_UNAVAILABLE_LABEL
            assert label.strip(), "the degradation label must be a visible, non-empty string"
        else:
            assert report.retrieval_supported is True
            assert report.degraded is False
            assert report.degradations == ()

    def test_the_label_is_specific(self):
        # A label people ignore is no label — it must name the step and the remedy.
        low = RETRIEVAL_UNAVAILABLE_LABEL.lower()
        assert "retrieval" in low
        assert "embedding" in low

    @pytest.mark.parametrize("generation_mode", ALL_MODES)
    def test_generation_mode_does_not_change_retrieval_capability(self, generation_mode):
        # Retrieval keys on the EMBEDDING mode, resolved independently — a
        # deployment generating in-boundary while embedding on hosted is still
        # correctly reported retrieval-degraded.
        assert retrieval_supported("hosted") is False
        assert retrieval_supported("in_boundary") is True
        hosted_emb = assembly_mode_report(generation_mode=generation_mode, embedding_mode="hosted")
        assert hosted_emb.degraded is True

    def test_active_modes_read_from_the_gateway(self, monkeypatch):
        monkeypatch.setenv(_GEN_ENV, "in_boundary")
        monkeypatch.setenv(_EMB_ENV, "customer_tenant")
        assert active_generation_mode() == "in_boundary"
        assert active_embedding_mode() == "customer_tenant"

    def test_resolution_failure_fails_safe_to_hosted(self, monkeypatch):
        # An unknown provider name makes the gateway raise; the capability read
        # must fail SAFE to hosted (the most degraded reading) — never hide a
        # degradation by reporting a more-capable mode than is active.
        monkeypatch.setenv(_EMB_ENV, "does-not-exist")
        assert active_embedding_mode() == "hosted"
        assert retrieval_supported() is False


# ---------------------------------------------------------------------------
# The label is surfaced ON THE FINDING (GraphContext), not only in a report
# ---------------------------------------------------------------------------

class TestDegradationSurfacedOnFinding:
    _ENTITIES = [
        {"entity_id": "e-1", "display_name": "Payments Team",
         "resolution_status": "resolved"},
    ]

    def test_hosted_embedding_finding_carries_the_retrieval_label(self, monkeypatch):
        monkeypatch.setenv(_EMB_ENV, "hosted")
        gc = build_graph_context(None, "run-t6", entities=self._ENTITIES, relationships=[])
        steps = [d["step"] for d in gc.mode_degradations]
        assert STEP_RETRIEVAL in steps
        labels = [d["label"] for d in gc.mode_degradations]
        assert RETRIEVAL_UNAVAILABLE_LABEL in labels

    @pytest.mark.parametrize("embedding_mode", ["in_boundary", "customer_tenant"])
    def test_embedding_capable_finding_has_no_degradation(self, monkeypatch, embedding_mode):
        monkeypatch.setenv(_EMB_ENV, embedding_mode)
        gc = build_graph_context(None, "run-t6", entities=self._ENTITIES, relationships=[])
        assert gc.mode_degradations == []

    def test_the_field_is_additive_and_defaults_empty(self):
        from app.graph_context import GraphContext

        assert GraphContext().mode_degradations == []


# ---------------------------------------------------------------------------
# Purity — the capability reader stays a pure, dependency-light module
# ---------------------------------------------------------------------------

class TestPurity:
    FORBIDDEN = ("psycopg2", "sqlalchemy", "app.db", "anthropic",
                 "requests", "httpx", "datetime", "time", "random")

    def test_module_level_imports_are_clean(self):
        """`mode_parity` reads only provider NAMES from the gateway, and does so
        lazily inside a function — so at module scope it pulls in nothing stateful."""
        src = Path(mp.__file__).read_text(encoding="utf-8")
        tree = ast.parse(src)
        module_level = set()
        for node in tree.body:  # top-level statements only
            if isinstance(node, ast.Import):
                module_level.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                module_level.add(node.module or "")
        for bad in self.FORBIDDEN:
            assert not any(bad in m for m in module_level), (
                f"mode_parity must stay pure at import time; found {bad!r}"
            )
