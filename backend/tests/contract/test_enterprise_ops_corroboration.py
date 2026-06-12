"""
ENT-5 / AT-266 (T5) — Contract tests: corroboration wiring + entity graph integration.

Covers:
  AC9  — ENT_INCIDENT_RESOLUTION_LAG confidence elevates MEDIUM → HIGH when
          COR-06 Slack escalation fires in the same window (ENT-2).
  AC5  — ENT_SLA_BREACH_BY_TEAM reads entity-graph corroboration result from
          raw_evidence (ENT-1); exact-name fallback stays MEDIUM.
  AC8  — End-to-end: run_pipeline() with mocked SN + Jira data produces at
          least one OpportunityCandidate.
  AC10 — Org-scope guard: COR-06 from a different org does not elevate.
"""
from __future__ import annotations

import pytest
from unittest.mock import patch, MagicMock

try:
    from backend.discovery.models import DetectorResult
    from backend.discovery.packs.enterprise_ops_scorer import (
        score_enterprise_ops,
        _cor06_fired,
    )
except ModuleNotFoundError:
    from discovery.models import DetectorResult
    from discovery.packs.enterprise_ops_scorer import (
        score_enterprise_ops,
        _cor06_fired,
    )


# ── fixture helpers ────────────────────────────────────────────────────────────

def _make_dr(detector_id: str, metric_value: float = 0.5, raw_evidence: dict | None = None) -> DetectorResult:
    return DetectorResult(
        detector_id=detector_id,
        signal_source="servicenow",
        metric_value=metric_value,
        threshold=0.30,
        raw_evidence=raw_evidence or {"metric": metric_value},
    )


def _lag_dr(raw_evidence: dict | None = None) -> DetectorResult:
    return _make_dr("ENT_INCIDENT_RESOLUTION_LAG", metric_value=0.50, raw_evidence=raw_evidence)


def _sla_dr(raw_evidence: dict | None = None) -> DetectorResult:
    ev = raw_evidence if raw_evidence is not None else {
        "top_team_breach_pct": 0.62,
        "top_team_breach_rate": 0.45,
        "top_team_name": "Commercial Credit",
        "org_breach_rate": 0.18,
        "teams_analysed": 5,
        "team_entity_resolved": False,
        "top_team_jira_open_issues": 0,
        "jira_corroborated": False,
        "match_strategy": "none",
        "confidence": "MEDIUM",
    }
    return DetectorResult(
        detector_id="ENT_SLA_BREACH_BY_TEAM",
        signal_source="servicenow",
        metric_value=0.62,
        threshold=0.40,
        raw_evidence=ev,
    )


def _cor06_sn(fired: bool, org_id: str = "org-1") -> dict:
    return {"cor06_slack_escalation": {"fired": fired, "org_id": org_id}}


# ── AC9: COR-06 confidence elevation ──────────────────────────────────────────

class TestAC9IncidentLagCorroboration:
    """ENT_INCIDENT_RESOLUTION_LAG MEDIUM → HIGH via COR-06 (ENT-2)."""

    def test_cor06_fired_elevates_to_high(self):
        dr = _lag_dr()
        result = score_enterprise_ops(dr, sn_data=_cor06_sn(fired=True), org_id="org-1")
        assert result["confidence"] == "HIGH"

    def test_cor06_fired_sets_corroborated_true(self):
        dr = _lag_dr()
        result = score_enterprise_ops(dr, sn_data=_cor06_sn(fired=True), org_id="org-1")
        assert result["corroborated"] is True

    def test_cor06_fired_sets_corroboration_source_slack(self):
        dr = _lag_dr()
        result = score_enterprise_ops(dr, sn_data=_cor06_sn(fired=True), org_id="org-1")
        assert result["corroboration_sources"] == ["Slack"]

    def test_cor06_not_fired_stays_medium(self):
        dr = _lag_dr()
        result = score_enterprise_ops(dr, sn_data=_cor06_sn(fired=False), org_id="org-1")
        assert result["confidence"] == "MEDIUM"

    def test_cor06_not_fired_corroborated_false(self):
        dr = _lag_dr()
        result = score_enterprise_ops(dr, sn_data=_cor06_sn(fired=False), org_id="org-1")
        assert result["corroborated"] is False
        assert result["corroboration_sources"] == []

    def test_no_sn_data_stays_medium(self):
        dr = _lag_dr()
        result = score_enterprise_ops(dr, sn_data=None, org_id="org-1")
        assert result["confidence"] == "MEDIUM"
        assert result["corroborated"] is False

    def test_missing_sn_data_key_stays_medium(self):
        dr = _lag_dr()
        result = score_enterprise_ops(dr, sn_data={"other_key": {}}, org_id="org-1")
        assert result["confidence"] == "MEDIUM"

    def test_malformed_cor06_block_stays_medium(self):
        # cor06_slack_escalation is a string, not a dict
        dr = _lag_dr()
        result = score_enterprise_ops(
            dr,
            sn_data={"cor06_slack_escalation": "yes"},
            org_id="org-1",
        )
        assert result["confidence"] == "MEDIUM"

    def test_cor06_missing_fired_field_stays_medium(self):
        dr = _lag_dr()
        result = score_enterprise_ops(
            dr,
            sn_data={"cor06_slack_escalation": {"org_id": "org-1"}},
            org_id="org-1",
        )
        assert result["confidence"] == "MEDIUM"

    def test_no_org_id_with_cor06_fires_elevates(self):
        # When org_id is not provided no org-scope check runs — still elevates.
        dr = _lag_dr()
        result = score_enterprise_ops(dr, sn_data=_cor06_sn(fired=True))
        assert result["confidence"] == "HIGH"

    def test_score_debug_contains_confidence_note(self):
        dr = _lag_dr()
        result = score_enterprise_ops(dr, sn_data=_cor06_sn(fired=True), org_id="org-1")
        assert "COR-06" in result["score_debug"]["confidence_note"]

    def test_score_debug_base_confidence_is_medium(self):
        dr = _lag_dr()
        result = score_enterprise_ops(dr, sn_data=_cor06_sn(fired=True), org_id="org-1")
        assert result["score_debug"]["base_confidence"] == "MEDIUM"

    def test_other_fields_unchanged_when_elevated(self):
        dr = _lag_dr()
        result = score_enterprise_ops(dr, sn_data=_cor06_sn(fired=True), org_id="org-1")
        assert result["tier"] == "Strategic"
        assert result["impact"] == 7
        assert result["effort"] == 3


# ── AC10: org-scope guard ─────────────────────────────────────────────────────

class TestAC10OrgScopeGuard:
    """COR-06 from a different org must not elevate confidence."""

    def test_wrong_org_blocks_elevation(self):
        dr = _lag_dr()
        sn = _cor06_sn(fired=True, org_id="org-EVIL")
        result = score_enterprise_ops(dr, sn_data=sn, org_id="org-1")
        assert result["confidence"] == "MEDIUM"

    def test_wrong_org_corroborated_false(self):
        dr = _lag_dr()
        sn = _cor06_sn(fired=True, org_id="org-EVIL")
        result = score_enterprise_ops(dr, sn_data=sn, org_id="org-1")
        assert result["corroborated"] is False

    def test_same_org_elevates(self):
        dr = _lag_dr()
        sn = _cor06_sn(fired=True, org_id="org-1")
        result = score_enterprise_ops(dr, sn_data=sn, org_id="org-1")
        assert result["confidence"] == "HIGH"

    def test_cor06_no_org_field_with_caller_org_elevates(self):
        # COR-06 block has no org_id field — no mismatch possible, elevation runs.
        dr = _lag_dr()
        sn = {"cor06_slack_escalation": {"fired": True}}
        result = score_enterprise_ops(dr, sn_data=sn, org_id="org-1")
        assert result["confidence"] == "HIGH"


# ── AC5: ENT-1 entity graph wiring ────────────────────────────────────────────

class TestAC5SLABreachEntityGraph:
    """ENT_SLA_BREACH_BY_TEAM reads ENT-1 corroboration from raw_evidence."""

    def _high_ev(self, team: str = "Commercial Credit", open_issues: int = 25) -> dict:
        return {
            "top_team_breach_pct": 0.62,
            "top_team_breach_rate": 0.45,
            "top_team_name": team,
            "org_breach_rate": 0.18,
            "teams_analysed": 5,
            "team_entity_resolved": True,
            "top_team_jira_open_issues": open_issues,
            "jira_corroborated": True,
            "match_strategy": "entity_graph",
            "confidence": "HIGH",
        }

    def _medium_ev(self, strategy: str = "exact_name") -> dict:
        return {
            "top_team_breach_pct": 0.55,
            "top_team_breach_rate": 0.30,
            "top_team_name": "Engineering",
            "org_breach_rate": 0.15,
            "teams_analysed": 4,
            "team_entity_resolved": False,
            "top_team_jira_open_issues": 12,
            "jira_corroborated": False,
            "match_strategy": strategy,
            "confidence": "MEDIUM",
        }

    def test_entity_graph_path_elevates_to_high(self):
        dr = _sla_dr(self._high_ev())
        result = score_enterprise_ops(dr)
        assert result["confidence"] == "HIGH"

    def test_entity_graph_path_sets_corroborated_true(self):
        dr = _sla_dr(self._high_ev())
        result = score_enterprise_ops(dr)
        assert result["corroborated"] is True

    def test_entity_graph_path_sets_source_jira(self):
        dr = _sla_dr(self._high_ev())
        result = score_enterprise_ops(dr)
        assert result["corroboration_sources"] == ["Jira"]

    def test_exact_name_fallback_stays_medium(self):
        dr = _sla_dr(self._medium_ev(strategy="exact_name"))
        result = score_enterprise_ops(dr)
        assert result["confidence"] == "MEDIUM"

    def test_no_match_stays_medium(self):
        dr = _sla_dr(self._medium_ev(strategy="none"))
        result = score_enterprise_ops(dr)
        assert result["confidence"] == "MEDIUM"

    def test_exact_name_fallback_corroborated_false(self):
        dr = _sla_dr(self._medium_ev())
        result = score_enterprise_ops(dr)
        assert result["corroborated"] is False
        assert result["corroboration_sources"] == []

    def test_missing_confidence_in_raw_evidence_stays_medium(self):
        ev = dict(self._high_ev())
        del ev["confidence"]
        dr = DetectorResult(
            detector_id="ENT_SLA_BREACH_BY_TEAM",
            signal_source="servicenow",
            metric_value=0.62,
            threshold=0.40,
            raw_evidence=ev,
        )
        result = score_enterprise_ops(dr)
        assert result["confidence"] == "MEDIUM"

    def test_score_debug_contains_team_name(self):
        dr = _sla_dr(self._high_ev(team="Commercial Credit"))
        result = score_enterprise_ops(dr)
        assert "Commercial Credit" in result["score_debug"]["confidence_note"]

    def test_score_debug_contains_open_issues(self):
        dr = _sla_dr(self._high_ev(open_issues=30))
        result = score_enterprise_ops(dr)
        assert "30" in result["score_debug"]["confidence_note"]

    def test_tier_and_impact_unchanged_when_elevated(self):
        dr = _sla_dr(self._high_ev())
        result = score_enterprise_ops(dr)
        assert result["tier"] == "Quick Win"
        assert result["impact"] == 7
        assert result["effort"] == 2


# ── _cor06_fired unit tests ───────────────────────────────────────────────────

class TestCor06FiredHelper:
    """Unit tests for the _cor06_fired() helper."""

    def test_fired_true_returns_true(self):
        assert _cor06_fired({"cor06_slack_escalation": {"fired": True}}) is True

    def test_fired_false_returns_false(self):
        assert _cor06_fired({"cor06_slack_escalation": {"fired": False}}) is False

    def test_empty_dict_returns_false(self):
        assert _cor06_fired({}) is False

    def test_none_sn_data_returns_false(self):
        assert _cor06_fired(None) is False

    def test_non_dict_block_returns_false(self):
        assert _cor06_fired({"cor06_slack_escalation": True}) is False

    def test_org_mismatch_blocks(self):
        sn = {"cor06_slack_escalation": {"fired": True, "org_id": "org-X"}}
        assert _cor06_fired(sn, org_id="org-1") is False

    def test_org_match_allows(self):
        sn = {"cor06_slack_escalation": {"fired": True, "org_id": "org-1"}}
        assert _cor06_fired(sn, org_id="org-1") is True

    def test_no_org_id_in_block_allows_elevation(self):
        sn = {"cor06_slack_escalation": {"fired": True}}
        assert _cor06_fired(sn, org_id="org-1") is True

    def test_caller_no_org_id_allows_elevation(self):
        sn = {"cor06_slack_escalation": {"fired": True, "org_id": "org-1"}}
        assert _cor06_fired(sn) is True

    def test_missing_fired_key_returns_false(self):
        assert _cor06_fired({"cor06_slack_escalation": {"org_id": "org-1"}}) is False


# ── AC8: end-to-end detector→scorer pipeline produces OpportunityCandidate ────

class TestAC8EndToEnd:
    """Detector + scorer pipeline with mocked SN+Jira data produces OpportunityCandidate dicts.

    Tests the full data path: detect() → DetectorResult → score_enterprise_ops()
    → opportunity dict with confidence, tier, impact, corroboration fields.
    The runner wiring (is_enterprise_ops_pack branch) is validated separately in
    TestAC8RunnerWiring.
    """

    def _sn_data(self, *, cor06: bool = False) -> dict:
        return {
            "incident_resolution": {
                "closed_incidents": [
                    {"jira_issue_key": f"JIRA-{i}", "closed_at": "2024-01-15"}
                    for i in range(1, 16)
                ],
                "as_of": "2024-02-01",
            },
            "sla_breach_by_team": {
                "teams": [
                    {"team": "Alpha Team",  "total_tickets": 40, "breached": 26},
                    {"team": "Beta Team",   "total_tickets": 30, "breached": 5},
                    {"team": "Gamma Team",  "total_tickets": 25, "breached": 3},
                    {"team": "Delta Team",  "total_tickets": 20, "breached": 2},
                ],
            },
            "cor06_slack_escalation": {"fired": cor06, "org_id": "demo-org"},
        }

    def _jira_data(self) -> dict:
        return {
            "issue_resolution": {
                "issues": {
                    f"JIRA-{i}": {"status": "In Progress", "resolved": False}
                    for i in range(1, 16)
                }
            },
            "team_backlog": {
                "open_issues_by_team": {"Alpha Team": 27},
            },
        }

    def _run_lag(self, *, cor06: bool = False) -> dict:
        try:
            from backend.discovery.detectors.ent_incident_resolution_lag import detect
        except ModuleNotFoundError:
            from discovery.detectors.ent_incident_resolution_lag import detect

        sn = self._sn_data(cor06=cor06)
        jira = self._jira_data()
        results = detect(sn_data=sn, jira_data=jira)
        assert results, "ENT_INCIDENT_RESOLUTION_LAG did not fire with test fixtures"
        return score_enterprise_ops(results[0], sn_data=sn, org_id="demo-org")

    def _run_sla(self, *, entity_overlay: bool = False) -> dict:
        try:
            from backend.discovery.detectors.ent_sla_breach_by_team import detect
        except ModuleNotFoundError:
            from discovery.detectors.ent_sla_breach_by_team import detect

        sn = self._sn_data()
        jira = self._jira_data()
        if entity_overlay:
            jira["team_entity_overlay"] = {
                "Alpha Team": "ENTITY_TEAM_001",
                "alpha team": "ENTITY_TEAM_001",
            }
        results = detect(sn_data=sn, jira_data=jira)
        assert results, "ENT_SLA_BREACH_BY_TEAM did not fire with test fixtures"
        return score_enterprise_ops(results[0], jira_data=jira, org_id="demo-org")

    # Lag detector + scorer pipeline ──────────────────────────────────────────

    def test_lag_pipeline_produces_opportunity_dict(self):
        result = self._run_lag()
        assert isinstance(result, dict)

    def test_lag_pipeline_has_required_fields(self):
        result = self._run_lag()
        for field in ("tier", "confidence", "impact", "effort", "corroborated", "corroboration_sources"):
            assert field in result, f"Missing field: {field}"

    def test_lag_pipeline_base_confidence_medium(self):
        result = self._run_lag(cor06=False)
        assert result["confidence"] == "MEDIUM"

    def test_lag_pipeline_cor06_elevates_to_high(self):
        result = self._run_lag(cor06=True)
        assert result["confidence"] == "HIGH"

    def test_lag_pipeline_cor06_sets_corroborated(self):
        result = self._run_lag(cor06=True)
        assert result["corroborated"] is True
        assert result["corroboration_sources"] == ["Slack"]

    def test_lag_pipeline_tier_is_strategic(self):
        result = self._run_lag()
        assert result["tier"] == "Strategic"

    def test_lag_pipeline_impact_is_7(self):
        result = self._run_lag()
        assert result["impact"] == 7

    # SLA breach detector + scorer pipeline ───────────────────────────────────

    def test_sla_pipeline_produces_opportunity_dict(self):
        result = self._run_sla()
        assert isinstance(result, dict)

    def test_sla_pipeline_has_required_fields(self):
        result = self._run_sla()
        for field in ("tier", "confidence", "impact", "effort", "corroborated", "corroboration_sources"):
            assert field in result, f"Missing field: {field}"

    def test_sla_pipeline_base_confidence_medium(self):
        result = self._run_sla(entity_overlay=False)
        assert result["confidence"] == "MEDIUM"

    def test_sla_pipeline_entity_overlay_elevates_to_high(self):
        result = self._run_sla(entity_overlay=True)
        assert result["confidence"] == "HIGH"

    def test_sla_pipeline_entity_overlay_corroborated(self):
        result = self._run_sla(entity_overlay=True)
        assert result["corroborated"] is True
        assert result["corroboration_sources"] == ["Jira"]

    def test_sla_pipeline_tier_is_quick_win(self):
        result = self._run_sla()
        assert result["tier"] == "Quick Win"


class TestAC8RunnerWiring:
    """Verify runner.py branches correctly to enterprise_ops scorer."""

    def test_is_enterprise_ops_pack_recognises_pack_id(self):
        try:
            from backend.discovery.packs.pack_config import is_enterprise_ops_pack
        except ModuleNotFoundError:
            from discovery.packs.pack_config import is_enterprise_ops_pack
        assert is_enterprise_ops_pack("enterprise_ops") is True
        assert is_enterprise_ops_pack("service_cloud") is False

    def test_is_enterprise_ops_detector_recognises_all_three(self):
        try:
            from backend.discovery.packs.enterprise_ops_scorer import is_enterprise_ops_detector
        except ModuleNotFoundError:
            from discovery.packs.enterprise_ops_scorer import is_enterprise_ops_detector
        for did in ("ENT_INCIDENT_RESOLUTION_LAG", "ENT_CHANGE_INCIDENT_CORRELATION", "ENT_SLA_BREACH_BY_TEAM"):
            assert is_enterprise_ops_detector(did) is True

    def test_scorer_importable_from_runner_import_path(self):
        try:
            from backend.discovery.packs.enterprise_ops_scorer import score_enterprise_ops
        except ModuleNotFoundError:
            from discovery.packs.enterprise_ops_scorer import score_enterprise_ops
        assert callable(score_enterprise_ops)
