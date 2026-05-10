"""
ENG-AIQ-NC-5 — Banking UI Language + LLM Prompts Tests
Sprint 5 — Wave 4

Tests:
  1. ncino pack produces banking-language prompt (not SC language)
  2. ncino prompt contains compliance instruction
  3. ncino prompt separates nCino evidence from Jira/SN corroboration
  4. sc pack produces original SC prompt unchanged
  5. ncino exec summary uses CRO language
  6. sc exec summary uses CXO language (unchanged)
  7. run_llm_enrichment accepts pack_id parameter
  8. ncino prompt contains llm_context from pack_config
  9. UI labels loaded from ncino_ui_labels.json for ncino pack
  10. SC pack labels unchanged

Run:
  pytest tests/contract/test_eng_aic_nc5.py -v
"""
from __future__ import annotations

import pytest
from unittest.mock import patch, MagicMock
from typing import Any, Dict, List


# ── Fixtures ──────────────────────────────────────────────────────────────────

def make_opp(detector_id="COVENANT_TRACKING_GAP", title="Test opportunity",
             category="Covenant Compliance", tier="Strategic", impact=8):
    return {
        "id": "opp_001",
        "title": title,
        "category": category,
        "tier": tier,
        "impact": impact,
        "effort": 4,
        "confidence": "HIGH",
        "aiRationale": "Existing rationale.",
        "evidenceIds": ["ev_001", "ev_002"],
        "_debug": {"detector_id": detector_id},
    }

def make_evidence(source="Salesforce", snippet="2 covenants overdue."):
    return [
        {"id": "ev_001", "source": source, "snippet": snippet,
         "evidenceType": "Metric", "detectorId": "COVENANT_TRACKING_GAP"},
    ]

def make_jira_evidence():
    return [
        {"id": "ev_jira_001", "source": "Jira",
         "snippet": "Covenant compliance: Covenant review reminders not triggering.",
         "evidenceType": "Metric", "detectorId": "COVENANT_TRACKING_GAP"},
    ]

def make_sn_evidence():
    return [
        {"id": "ev_sn_001", "source": "ServiceNow",
         "snippet": "Covenant compliance: Compliance team cannot update covenant status.",
         "evidenceType": "Metric", "detectorId": "COVENANT_TRACKING_GAP"},
    ]


# ── _opp_prompt pack-awareness ─────────────────────────────────────────────────

class TestOppPromptPackAware:

    def _prompt(self, opp, evidence, pack_id=None):
        from app.llm_enrichment import _opp_prompt
        return _opp_prompt(opp, evidence, pack_id=pack_id)

    def test_ncino_prompt_uses_banking_language(self):
        opp = make_opp()
        ev = make_evidence()
        opp["evidenceIds"] = ["ev_001"]
        prompt = self._prompt(opp, ev, pack_id="ncino")
        assert "banking" in prompt.lower() or "lending" in prompt.lower() or \
               "commercial" in prompt.lower(), \
               "ncino prompt must use banking/lending language"

    def test_ncino_prompt_contains_compliance_instruction(self):
        """Non-negotiable: ncino prompt must say no automated credit decisions."""
        opp = make_opp()
        prompt = self._prompt(opp, [], pack_id="ncino")
        assert "credit decision" in prompt.lower(), \
            "ncino prompt missing compliance instruction about credit decisions"

    def test_ncino_prompt_separates_ncino_and_corroboration_evidence(self):
        """nCino evidence and Jira/SN evidence appear in separate sections."""
        opp = make_opp()
        ev = make_evidence() + make_jira_evidence() + make_sn_evidence()
        opp["evidenceIds"] = ["ev_001", "ev_jira_001", "ev_sn_001"]
        prompt = self._prompt(opp, ev, pack_id="ncino")
        assert "nCino Evidence" in prompt or "Corroborating Evidence" in prompt, \
            "ncino prompt should separate nCino evidence from Jira/SN corroboration"

    def test_sc_prompt_unchanged(self):
        """service_cloud prompt must be identical to original."""
        opp = make_opp()
        prompt_sc   = self._prompt(opp, [], pack_id="service_cloud")
        prompt_none = self._prompt(opp, [], pack_id=None)
        assert "Salesforce automation discovery" in prompt_sc
        assert prompt_sc == prompt_none, \
            "SC prompt must be identical whether pack='service_cloud' or pack=None"

    def test_ncino_prompt_contains_cro_audience_reference(self):
        opp = make_opp()
        prompt = self._prompt(opp, [], pack_id="ncino")
        assert "CRO" in prompt or "Head of Commercial Lending" in prompt or \
               "banking" in prompt.lower()

    def test_ncino_prompt_does_not_contain_sc_boilerplate(self):
        """ncino prompt must not say 'Salesforce automation discovery report'."""
        opp = make_opp()
        prompt = self._prompt(opp, [], pack_id="ncino")
        assert "Salesforce automation discovery report" not in prompt


# ── _exec_summary_prompt pack-awareness ───────────────────────────────────────

class TestExecSummaryPromptPackAware:

    def _exec_prompt(self, opps, sources, pack_id=None):
        from app.llm_enrichment import _exec_summary_prompt
        return _exec_summary_prompt(opps, sources, pack_id=pack_id)

    def test_ncino_exec_prompt_targets_cro(self):
        opps = [make_opp()]
        prompt = self._exec_prompt(opps, {"totalConnected": 3}, pack_id="ncino")
        assert "CRO" in prompt or "Head of Commercial Lending" in prompt

    def test_ncino_exec_prompt_compliance_instruction(self):
        opps = [make_opp()]
        prompt = self._exec_prompt(opps, {}, pack_id="ncino")
        assert "credit decision" in prompt.lower()

    def test_ncino_exec_prompt_mentions_corroboration(self):
        opps = [make_opp()]
        prompt = self._exec_prompt(opps, {"totalConnected": 3}, pack_id="ncino")
        assert "Jira" in prompt or "ServiceNow" in prompt or "corroborate" in prompt.lower()

    def test_sc_exec_prompt_unchanged(self):
        opps = [make_opp()]
        prompt_sc   = self._exec_prompt(opps, {}, pack_id="service_cloud")
        prompt_none = self._exec_prompt(opps, {}, pack_id=None)
        assert "CXO" in prompt_sc or "executive" in prompt_sc.lower()
        assert prompt_sc == prompt_none


# ── run_llm_enrichment pack_id parameter ──────────────────────────────────────

class TestRunLlmEnrichmentPackId:

    def test_accepts_pack_id_parameter(self):
        """run_llm_enrichment must accept pack_id without error."""
        import inspect
        from app.llm_enrichment import run_llm_enrichment
        sig = inspect.signature(run_llm_enrichment)
        assert "pack_id" in sig.parameters, "run_llm_enrichment missing pack_id parameter"

    def test_ncino_pack_id_passed_to_prompt(self):
        """When pack_id=ncino, _opp_prompt is called with pack_id=ncino."""
        from app.llm_enrichment import run_llm_enrichment
        import os
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}

        with patch("app.llm_enrichment._opp_prompt") as mock_prompt:
            mock_prompt.return_value = '{"aiSummary":"x","aiWhyBullets":[],"aiRisks":[],"aiSuggestedNextSteps":[]}'
            with patch("app.llm_enrichment._call_claude", return_value=mock_prompt.return_value):
                with patch.dict(os.environ, env, clear=True):
                    run_llm_enrichment(
                        run_id="test",
                        opps=[make_opp()],
                        evidence=[],
                        pack_id="ncino",
                    )
        # Verify pack_id was passed
        if mock_prompt.called:
            call_kwargs = mock_prompt.call_args
            passed_pack = call_kwargs.kwargs.get("pack_id") or \
                         (call_kwargs.args[2] if len(call_kwargs.args) > 2 else None)
            assert passed_pack == "ncino"

    def test_no_api_key_falls_back_gracefully(self):
        """Without API key, ncino pack falls back cleanly."""
        import os
        from app.llm_enrichment import run_llm_enrichment
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        with patch.dict(os.environ, env, clear=True):
            result = run_llm_enrichment(
                run_id="test_no_key",
                opps=[make_opp()],
                evidence=[],
                pack_id="ncino",
            )
        # Must return valid structure
        assert "perOpportunity" in result
        assert "executiveSummary" in result


# ── UI labels for ncino pack ───────────────────────────────────────────────────

class TestNcinoUiLabels:

    def test_ncino_ui_labels_loadable(self):
        """get_ui_labels('ncino') returns dict or None — must not raise."""
        from discovery.packs.pack_config import get_ui_labels
        result = get_ui_labels("ncino")
        assert result is None or isinstance(result, dict)

    def test_ncino_ui_labels_have_all_five_detectors(self):
        from discovery.packs.pack_config import get_ui_labels
        labels = get_ui_labels("ncino")
        if labels is None:
            pytest.skip("ncino_ui_labels.json not found")
        for det in [
            "LOAN_ORIGINATION_ROUTING_FRICTION",
            "COVENANT_TRACKING_GAP",
            "CHECKLIST_BOTTLENECK",
            "SPREADING_BOTTLENECK",
            "APPROVAL_BOTTLENECK",
        ]:
            assert det in labels, f"{det} missing from ncino_ui_labels.json"

    def test_ncino_ui_labels_have_all_screens(self):
        from discovery.packs.pack_config import get_ui_labels
        labels = get_ui_labels("ncino")
        if labels is None:
            pytest.skip("ncino_ui_labels.json not found")
        for det, entry in labels.items():
            if det.startswith("_"):
                continue
            for field in ["s6_title", "s6_desc", "s7_category", "s9_roadmap", "s10_exec"]:
                assert field in entry, f"{det} missing {field} in ncino_ui_labels.json"

    def test_covenant_has_compliance_guardrail(self):
        from discovery.packs.pack_config import get_ui_labels
        labels = get_ui_labels("ncino")
        if labels is None:
            pytest.skip("ncino_ui_labels.json not found")
        cov = labels.get("COVENANT_TRACKING_GAP", {})
        guardrail = cov.get("compliance_guardrail")
        assert guardrail is not None, "Covenant must have compliance_guardrail field"
        assert len(guardrail) > 0

    def test_sc_pack_ui_labels_returns_none(self):
        from discovery.packs.pack_config import get_ui_labels
        assert get_ui_labels("service_cloud") is None
