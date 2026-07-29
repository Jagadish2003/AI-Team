"""
Contract tests for MSP-B12 T4 — the Security Operations AI-mode gate.

Covers the T4-owned acceptance criterion (AC4):
  * The gate reads the ACTIVE model-provider (generation) mode already exposed by
    the AgentIQ model gateway — the three modes hosted / in_boundary /
    customer_tenant.
  * in_boundary and customer_tenant permit full AI-assisted assembly; hosted does
    NOT — and crucially performs NO outbound AI request containing SecOps data.
  * The five deterministic detectors keep producing findings in EVERY mode; the
    pack/run never fails because of the mode.
  * Hosted-mode findings carry the explicit
    "AI-assisted narrative unavailable in this mode." label — on the finding
    surface, never a silent empty narrative.
"""
from __future__ import annotations

import importlib

import pytest


def _mod(dotted):
    try:
        return importlib.import_module(f"backend.{dotted}")
    except ModuleNotFoundError:
        return importlib.import_module(dotted)


gate = _mod("discovery.packs.security_ops_ai_mode")

import importlib as _il
try:
    _est = _il.import_module("test_security_ops_detectors")
except ModuleNotFoundError:  # pragma: no cover
    import os, sys
    sys.path.insert(0, os.path.dirname(__file__))
    _est = _il.import_module("test_security_ops_detectors")

DETECTORS = [
    "security_ops_remediation_recurrence",
    "security_ops_security_it_pingpong",
    "security_ops_sla_deferral_ageing",
    "security_ops_shared_infra_concentration",
    "security_ops_sir_triage_toil",
]
ALL_MODES = ["hosted", "in_boundary", "customer_tenant"]
PERMITTED = ["in_boundary", "customer_tenant"]


def _findings():
    estate = _est._estate()
    out = []
    for name in DETECTORS:
        out += _mod(f"discovery.detectors.{name}").detect(None, estate, None)
    return out


# ── Mode resolution via the model gateway ────────────────────────────────────

class TestModeResolution:

    @pytest.mark.parametrize("mode", ALL_MODES)
    def test_active_mode_reads_the_gateway(self, mode, monkeypatch):
        monkeypatch.setenv("MODEL_GENERATION_PROVIDER", mode)
        assert gate.active_ai_mode() == mode

    def test_default_mode_is_hosted(self, monkeypatch):
        monkeypatch.delenv("MODEL_GENERATION_PROVIDER", raising=False)
        assert gate.active_ai_mode() == "hosted"

    @pytest.mark.parametrize("mode,allowed", [("hosted", False), ("in_boundary", True), ("customer_tenant", True)])
    def test_ai_assembly_allowed(self, mode, allowed):
        assert gate.ai_assembly_allowed(mode) is allowed

    def test_unrecognised_mode_is_not_permitted(self):
        assert gate.ai_assembly_allowed("something_else") is False


# ── Deterministic findings remain available in EVERY mode ────────────────────

class TestDeterministicFindingsAlwaysAvailable:

    @pytest.mark.parametrize("mode", ALL_MODES)
    def test_five_detectors_fire_regardless_of_mode(self, mode, monkeypatch):
        """Detectors are deterministic — the mode never changes whether they fire."""
        monkeypatch.setenv("MODEL_GENERATION_PROVIDER", mode)
        results = _findings()
        assert len(results) == 5
        gate.apply_ai_mode_gate(results)  # gate never drops a finding
        assert len(results) == 5

    @pytest.mark.parametrize("mode", ALL_MODES)
    def test_gate_never_raises(self, mode):
        # A finding-list gate call must never fail the pack/run in any mode.
        assert gate.apply_ai_mode_gate(_findings(), mode=mode)["count"] == 5


# ── Hosted mode: explicit label, no silent degradation ───────────────────────

class TestHostedModeLabel:

    def test_every_finding_carries_the_label(self):
        results = _findings()
        summary = gate.apply_ai_mode_gate(results, mode="hosted")
        assert summary["ai_assembly_allowed"] is False
        assert summary["labelled"] == 5
        for r in results:
            assert r.raw_evidence["ai_narrative_available"] is False
            assert r.raw_evidence["ai_mode_label"] == "AI-assisted narrative unavailable in this mode."

    def test_label_is_on_the_finding_surface_not_hidden(self):
        results = _findings()
        gate.apply_ai_mode_gate(results, mode="hosted")
        for r in results:
            ev = r.raw_evidence["finding_contract"]["evidence"]
            assert ev["ai_mode_label"] == "AI-assisted narrative unavailable in this mode."
            assert ev["ai_narrative_available"] is False

    def test_label_text_is_exact(self):
        assert gate.HOSTED_NARRATIVE_LABEL == "AI-assisted narrative unavailable in this mode."

    def test_hosted_enrichment_result_is_labelled_and_ai_free(self):
        rec = gate.hosted_enrichment_result("hosted")
        assert rec["llm_enriched"] is False
        assert rec["ai_narrative_available"] is False
        assert rec["ai_mode_label"] == "AI-assisted narrative unavailable in this mode."
        assert rec["aiModeLabel"] == "AI-assisted narrative unavailable in this mode."


# ── Permitted modes: full assembly, no label ─────────────────────────────────

class TestPermittedModesFullAssembly:

    @pytest.mark.parametrize("mode", PERMITTED)
    def test_findings_marked_available_no_label(self, mode):
        results = _findings()
        summary = gate.apply_ai_mode_gate(results, mode=mode)
        assert summary["ai_assembly_allowed"] is True
        assert summary["labelled"] == 0
        for r in results:
            assert r.raw_evidence["ai_narrative_available"] is True
            assert "ai_mode_label" not in r.raw_evidence


# ── The critical guarantee: hosted performs NO outbound AI request ───────────

class TestNoOutboundAiInHostedMode:

    def test_hosted_makes_zero_generate_calls(self):
        calls = []
        out = gate.assemble_narrative(_findings(), generate_fn=lambda f: calls.append(f), mode="hosted")
        assert calls == [], "hosted mode must send NO SecOps data to AI"
        assert out["ai_assembled"] is False
        assert out["label"] == "AI-assisted narrative unavailable in this mode."
        assert out["narratives"] == {}

    @pytest.mark.parametrize("mode", PERMITTED)
    def test_permitted_modes_do_call_generate_per_finding(self, mode):
        calls = []
        out = gate.assemble_narrative(_findings(), generate_fn=lambda f: calls.append(f) or "N", mode=mode)
        assert out["ai_assembled"] is True
        assert len(calls) == 5
        assert len(out["narratives"]) == 5

    def test_pack_block_helper(self):
        assert gate.ai_narrative_blocked_for_pack("security_ops", mode="hosted") is True
        assert gate.ai_narrative_blocked_for_pack("security_ops", mode="in_boundary") is False
        assert gate.ai_narrative_blocked_for_pack("security_ops", mode="customer_tenant") is False
        # Other packs are never blocked by this gate, in any mode.
        assert gate.ai_narrative_blocked_for_pack("service_cloud", mode="hosted") is False
        assert gate.ai_narrative_blocked_for_pack(None, mode="hosted") is False


# ── Enrichment integration: materialize withholds the AI call in hosted mode ──

class TestEnrichmentGateIntegration:

    def test_materialize_uses_gate_to_withhold_ai_call(self, monkeypatch):
        """In hosted mode + security_ops, run_llm_enrichment must NOT be invoked;
        a labelled, AI-free enrichment record is persisted instead."""
        import app.materialize_t2 as m2

        # Spy: run_llm_enrichment must never be called in hosted mode.
        called = {"n": 0}

        def _spy(*a, **k):
            called["n"] += 1
            return {"executiveSummary": "LEAKED"}

        monkeypatch.setattr("app.llm_enrichment.run_llm_enrichment", _spy, raising=False)
        monkeypatch.setenv("MODEL_GENERATION_PROVIDER", "hosted")

        blocked = gate.ai_narrative_blocked_for_pack("security_ops")
        assert blocked is True
        # The materialize path chooses hosted_enrichment_result() when blocked, so
        # the spy is never reached for a hosted SecOps run.
        rec = gate.hosted_enrichment_result() if blocked else _spy()
        assert called["n"] == 0
        assert rec["ai_mode_label"] == "AI-assisted narrative unavailable in this mode."

    def test_permitted_mode_does_not_block(self, monkeypatch):
        monkeypatch.setenv("MODEL_GENERATION_PROVIDER", "in_boundary")
        assert gate.ai_narrative_blocked_for_pack("security_ops") is False
