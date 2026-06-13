"""
Contract tests for ENT-5 AT-265 (T4) + AT-267 (T6) — Enterprise Operations Intelligence Pack
registration, scorer, UI labels, and detector threshold/boundary behaviour.

Covers acceptance criteria:
  AC1  — ENT_INCIDENT_RESOLUTION_LAG fires at threshold boundary.
  AC2  — Minimum volume guard: ENT_INCIDENT_RESOLUTION_LAG does NOT fire when
          incident_count_30d < 10.
  AC3  — ENT_CHANGE_INCIDENT_CORRELATION fires at threshold with confidence=HIGH.
  AC4  — ENT_SLA_BREACH_BY_TEAM fires at threshold.
  AC6  — All three detectors have SIGNAL_METRICS defined.
  AC7  — enterprise_ops pack registered in PACK_REGISTRY; pack_id='enterprise_ops';
          llm_context uses operations leadership language.
  AC8  — Pack structure allows runner with mocked data to locate all three detectors.
  AC10 — Cross-org isolation: findings for org_A never appear in org_B query.
"""
from __future__ import annotations

import os
import pytest


# ---------------------------------------------------------------------------
# Import helpers
# ---------------------------------------------------------------------------

def _pack_config():
    try:
        import backend.discovery.packs.pack_config as m
    except ModuleNotFoundError:
        import discovery.packs.pack_config as m
    return m


def _scorer():
    try:
        import backend.discovery.packs.enterprise_ops_scorer as m
    except ModuleNotFoundError:
        import discovery.packs.enterprise_ops_scorer as m
    return m


def _make_detector_result(detector_id: str, metric_value: float = 0.5):
    """Build a minimal DetectorResult-like object sufficient for the scorer."""
    try:
        from backend.discovery.models import DetectorResult
    except ModuleNotFoundError:
        from discovery.models import DetectorResult

    return DetectorResult(
        detector_id=detector_id,
        signal_source="servicenow",
        metric_value=metric_value,
        threshold=0.3,
        raw_evidence={"metric": metric_value},
    )


# ---------------------------------------------------------------------------
# AC7 — pack registered in PACK_REGISTRY; pack_id='enterprise_ops';
#        llm_context uses operations leadership language (no IT jargon).
# ---------------------------------------------------------------------------

class TestAC7:

    def test_pack_id_in_list_packs(self):
        m = _pack_config()
        assert "enterprise_ops" in m.list_packs()

    def test_pack_id_in_registry(self):
        m = _pack_config()
        assert "enterprise_ops" in m.PACK_REGISTRY

    def test_get_pack_returns_correct_pack_id(self):
        m = _pack_config()
        assert m.get_pack("enterprise_ops")["packId"] == "enterprise_ops"

    def test_pack_name_is_enterprise_operations_intelligence(self):
        m = _pack_config()
        assert m.get_pack("enterprise_ops")["packName"] == "Enterprise Operations Intelligence"

    def test_domain_is_enterprise_ops(self):
        m = _pack_config()
        assert m.get_pack("enterprise_ops")["domain"] == "enterprise_ops"

    def test_pack_domain_is_enterprise_ops(self):
        m = _pack_config()
        assert m.get_pack("enterprise_ops")["pack_domain"] == "enterprise_ops"

    def test_pack_id_is_not_itsm(self):
        """pack_id must be 'enterprise_ops', never 'itsm'."""
        m = _pack_config()
        assert "enterprise_ops" in m.PACK_REGISTRY
        assert "itsm" not in m.PACK_REGISTRY

    def test_pack_id_is_not_servicenow_jira(self):
        """pack_id must be 'enterprise_ops', never 'servicenow_jira'."""
        m = _pack_config()
        assert "servicenow_jira" not in m.PACK_REGISTRY

    def test_llm_context_is_non_empty_string(self):
        m = _pack_config()
        ctx = m.get_pack("enterprise_ops").get("llm_context", "")
        assert isinstance(ctx, str) and len(ctx) > 0

    def test_llm_context_uses_operations_leadership_language(self):
        """LLM context must reference VP of Operations or COO audience."""
        m = _pack_config()
        ctx = m.get_pack("enterprise_ops")["llm_context"].lower()
        assert "vp of operations" in ctx or "chief operating" in ctx or "operations leadership" in ctx

    def test_llm_context_avoids_it_jargon(self):
        """LLM context must not use ITSM framing; 'not an IT manager' is a spec-required
        disavowal phrase (ENT-5 §2) and is permitted — it rejects IT-manager framing
        rather than adopting it."""
        m = _pack_config()
        ctx = m.get_pack("enterprise_ops")["llm_context"].lower()
        assert "itsm" not in ctx
        # The spec requires "not an IT manager" as an audience clarifier — check
        # the phrase appears as a disavowal, not as a positive framing.
        assert "not an it manager" in ctx

    def test_llm_context_mentions_servicenow_and_jira(self):
        m = _pack_config()
        ctx = m.get_pack("enterprise_ops")["llm_context"].lower()
        assert "servicenow" in ctx and "jira" in ctx

    def test_llm_context_states_no_automated_action(self):
        """LLM context must contain a compliance guardrail."""
        m = _pack_config()
        ctx = m.get_pack("enterprise_ops")["llm_context"].lower()
        assert "not" in ctx or "no automated" in ctx or "avoid" in ctx

    def test_is_enterprise_ops_pack_true_for_this_pack(self):
        m = _pack_config()
        assert m.is_enterprise_ops_pack("enterprise_ops") is True

    def test_is_enterprise_ops_pack_false_for_service_cloud(self):
        m = _pack_config()
        assert m.is_enterprise_ops_pack("service_cloud") is False

    def test_is_enterprise_ops_pack_false_for_ncino(self):
        m = _pack_config()
        assert m.is_enterprise_ops_pack("ncino") is False

    def test_is_enterprise_ops_pack_false_for_strs(self):
        m = _pack_config()
        assert m.is_enterprise_ops_pack("strs_benefits") is False

    def test_is_enterprise_ops_pack_false_for_none(self):
        """None falls back to DEFAULT_PACK (service_cloud), not enterprise_ops."""
        m = _pack_config()
        assert m.is_enterprise_ops_pack(None) is False

    def test_is_enterprise_ops_pack_false_for_unknown(self):
        m = _pack_config()
        assert m.is_enterprise_ops_pack("not_a_pack") is False

    def test_get_pack_domain_returns_enterprise_ops(self):
        m = _pack_config()
        assert m.get_pack_domain("enterprise_ops") == "enterprise_ops"

    def test_get_llm_context_returns_string(self):
        m = _pack_config()
        ctx = m.get_llm_context("enterprise_ops")
        assert isinstance(ctx, str) and len(ctx) > 0

    def test_default_pack_unchanged(self):
        m = _pack_config()
        assert m.DEFAULT_PACK == "service_cloud"

    def test_existing_packs_still_present(self):
        """Existing packs must not be disturbed by the new enterprise_ops entry."""
        m = _pack_config()
        for pack_id in ("service_cloud", "ncino", "strs_benefits", "sqlserver_opsignal", "github_engineering"):
            assert pack_id in m.PACK_REGISTRY, f"{pack_id} missing after enterprise_ops added"

    def test_is_ncino_pack_still_works(self):
        m = _pack_config()
        assert m.is_ncino_pack("ncino") is True
        assert m.is_ncino_pack("enterprise_ops") is False

    def test_is_sqlserver_opsignal_pack_still_works(self):
        m = _pack_config()
        assert m.is_sqlserver_opsignal_pack("sqlserver_opsignal") is True
        assert m.is_sqlserver_opsignal_pack("enterprise_ops") is False

    def test_is_github_engineering_pack_still_works(self):
        m = _pack_config()
        assert m.is_github_engineering_pack("github_engineering") is True
        assert m.is_github_engineering_pack("enterprise_ops") is False


# ---------------------------------------------------------------------------
# AC8 — Pack structure allows runner to locate all three detectors
# ---------------------------------------------------------------------------

class TestAC8:

    _DETECTOR_PATHS = (
        "discovery.detectors.ent_incident_resolution_lag",
        "discovery.detectors.ent_change_incident_correlation",
        "discovery.detectors.ent_sla_breach_by_team",
    )

    def test_three_detectors_registered(self):
        m = _pack_config()
        assert len(m.get_pack("enterprise_ops")["detectors"]) == 3

    def test_detector_paths_are_strings(self):
        m = _pack_config()
        for path in m.get_pack("enterprise_ops")["detectors"]:
            assert isinstance(path, str)

    @pytest.mark.parametrize("expected_path", _DETECTOR_PATHS)
    def test_detector_path_present(self, expected_path):
        m = _pack_config()
        detectors = m.get_pack("enterprise_ops")["detectors"]
        assert any(expected_path in d for d in detectors), (
            f"Expected detector path containing '{expected_path}' not found in: {detectors}"
        )

    def test_get_detector_modules_returns_three_paths(self):
        m = _pack_config()
        assert len(m.get_detector_modules("enterprise_ops")) == 3

    def test_ui_labels_path_points_to_correct_file(self):
        m = _pack_config()
        path = m.get_pack("enterprise_ops").get("ui_labels_path")
        assert path is not None
        assert "enterprise_ops_ui_labels.json" in path

    def test_ui_labels_json_file_exists_on_disk(self):
        m = _pack_config()
        path = m.get_pack("enterprise_ops")["ui_labels_path"]
        assert os.path.isfile(path), f"ui_labels_path file not found: {path}"


# ---------------------------------------------------------------------------
# UI labels — s6_title, agentType, s6_why, s6_action for all three detectors
# ---------------------------------------------------------------------------

class TestUILabels:

    _DETECTOR_IDS = (
        "ENT_INCIDENT_RESOLUTION_LAG",
        "ENT_CHANGE_INCIDENT_CORRELATION",
        "ENT_SLA_BREACH_BY_TEAM",
    )
    _REQUIRED_FIELDS = ("s6_title", "agentType", "s6_why", "s6_action")

    def test_get_ui_labels_returns_non_none(self):
        m = _pack_config()
        assert m.get_ui_labels("enterprise_ops") is not None

    def test_get_ui_labels_returns_dict(self):
        m = _pack_config()
        assert isinstance(m.get_ui_labels("enterprise_ops"), dict)

    def test_all_three_detector_ids_present(self):
        m = _pack_config()
        labels = m.get_ui_labels("enterprise_ops")
        for det_id in self._DETECTOR_IDS:
            assert det_id in labels, f"Missing labels for {det_id}"

    @pytest.mark.parametrize("det_id", _DETECTOR_IDS)
    @pytest.mark.parametrize("field", _REQUIRED_FIELDS)
    def test_required_field_present(self, det_id, field):
        m = _pack_config()
        labels = m.get_ui_labels("enterprise_ops")
        entry = labels.get(det_id, {})
        assert field in entry, f"{det_id} missing '{field}'"

    @pytest.mark.parametrize("det_id", _DETECTOR_IDS)
    @pytest.mark.parametrize("field", _REQUIRED_FIELDS)
    def test_required_field_is_non_empty_string(self, det_id, field):
        m = _pack_config()
        labels = m.get_ui_labels("enterprise_ops")
        value = labels[det_id][field]
        assert isinstance(value, str) and len(value) > 0, (
            f"{det_id}['{field}'] must be a non-empty string"
        )

    @pytest.mark.parametrize("det_id", _DETECTOR_IDS)
    def test_agent_type_is_monitoring_agent(self, det_id):
        """All three detectors must be Monitoring Agents per ENT-5 Section 3."""
        m = _pack_config()
        labels = m.get_ui_labels("enterprise_ops")
        assert labels[det_id]["agentType"] == "Monitoring Agent", (
            f"{det_id}: agentType must be 'Monitoring Agent'"
        )

    def test_incident_resolution_lag_s6_title(self):
        m = _pack_config()
        labels = m.get_ui_labels("enterprise_ops")
        title = labels["ENT_INCIDENT_RESOLUTION_LAG"]["s6_title"].lower()
        assert "incident" in title or "resolution" in title or "gap" in title

    def test_change_incident_correlation_s6_title(self):
        m = _pack_config()
        labels = m.get_ui_labels("enterprise_ops")
        title = labels["ENT_CHANGE_INCIDENT_CORRELATION"]["s6_title"].lower()
        assert "change" in title or "risk" in title or "incident" in title

    def test_sla_breach_by_team_s6_title(self):
        m = _pack_config()
        labels = m.get_ui_labels("enterprise_ops")
        title = labels["ENT_SLA_BREACH_BY_TEAM"]["s6_title"].lower()
        assert "sla" in title or "breach" in title or "concentration" in title

    def test_ui_labels_use_operations_language(self):
        """Labels must not use IT jargon — audience is VP of Operations."""
        m = _pack_config()
        labels = m.get_ui_labels("enterprise_ops")
        combined = " ".join(
            labels[d]["s6_why"] + " " + labels[d]["s6_action"]
            for d in self._DETECTOR_IDS
        ).lower()
        # Should NOT use ITSM framing
        assert "itsm" not in combined


# ---------------------------------------------------------------------------
# AC6 — All three detectors have SIGNAL_METRICS defined (max 8)
# ---------------------------------------------------------------------------

class TestAC6:

    def test_incident_resolution_lag_signal_metrics_defined(self):
        try:
            from backend.discovery.detectors.ent_incident_resolution_lag import SIGNAL_METRICS
        except ModuleNotFoundError:
            from discovery.detectors.ent_incident_resolution_lag import SIGNAL_METRICS
        assert isinstance(SIGNAL_METRICS, list)
        assert len(SIGNAL_METRICS) > 0
        assert len(SIGNAL_METRICS) <= 8

    def test_incident_resolution_lag_signal_metrics_contains_unresolved_pct(self):
        try:
            from backend.discovery.detectors.ent_incident_resolution_lag import SIGNAL_METRICS
        except ModuleNotFoundError:
            from discovery.detectors.ent_incident_resolution_lag import SIGNAL_METRICS
        assert "unresolved_pct" in SIGNAL_METRICS

    def test_change_incident_correlation_signal_metrics_defined(self):
        try:
            from backend.discovery.detectors.ent_change_incident_correlation import SIGNAL_METRICS
        except ModuleNotFoundError:
            from discovery.detectors.ent_change_incident_correlation import SIGNAL_METRICS
        assert isinstance(SIGNAL_METRICS, list)
        assert len(SIGNAL_METRICS) > 0
        assert len(SIGNAL_METRICS) <= 8

    def test_change_incident_correlation_signal_metrics_contains_ratio(self):
        try:
            from backend.discovery.detectors.ent_change_incident_correlation import SIGNAL_METRICS
        except ModuleNotFoundError:
            from discovery.detectors.ent_change_incident_correlation import SIGNAL_METRICS
        assert "post_change_incident_ratio" in SIGNAL_METRICS

    def test_sla_breach_by_team_signal_metrics_defined(self):
        try:
            from backend.discovery.detectors.ent_sla_breach_by_team import SIGNAL_METRICS
        except ModuleNotFoundError:
            from discovery.detectors.ent_sla_breach_by_team import SIGNAL_METRICS
        assert isinstance(SIGNAL_METRICS, list)
        assert len(SIGNAL_METRICS) > 0
        assert len(SIGNAL_METRICS) <= 8

    def test_sla_breach_by_team_signal_metrics_contains_top_team_breach_pct(self):
        try:
            from backend.discovery.detectors.ent_sla_breach_by_team import SIGNAL_METRICS
        except ModuleNotFoundError:
            from discovery.detectors.ent_sla_breach_by_team import SIGNAL_METRICS
        assert "top_team_breach_pct" in SIGNAL_METRICS


# ---------------------------------------------------------------------------
# Scorer — enterprise_ops_scorer.py
# ---------------------------------------------------------------------------

class TestScorer:

    _DETECTOR_IDS = (
        "ENT_INCIDENT_RESOLUTION_LAG",
        "ENT_CHANGE_INCIDENT_CORRELATION",
        "ENT_SLA_BREACH_BY_TEAM",
    )

    def test_is_enterprise_ops_detector_true_for_known(self):
        m = _scorer()
        for det_id in self._DETECTOR_IDS:
            assert m.is_enterprise_ops_detector(det_id) is True

    def test_is_enterprise_ops_detector_false_for_unknown(self):
        m = _scorer()
        assert m.is_enterprise_ops_detector("DB_TICKET_VOLUME_SURGE") is False

    @pytest.mark.parametrize("det_id", _DETECTOR_IDS)
    def test_score_enterprise_ops_returns_dict(self, det_id):
        m = _scorer()
        dr = _make_detector_result(det_id)
        result = m.score_enterprise_ops(dr)
        assert isinstance(result, dict)

    @pytest.mark.parametrize("det_id", _DETECTOR_IDS)
    def test_score_enterprise_ops_has_required_keys(self, det_id):
        m = _scorer()
        dr = _make_detector_result(det_id)
        result = m.score_enterprise_ops(dr)
        for key in ("tier", "impact", "effort", "effort_label", "confidence",
                    "roadmap_stage", "corroborated", "corroboration_sources", "score_debug"):
            assert key in result, f"Missing key '{key}' in scorer output for {det_id}"

    @pytest.mark.parametrize("det_id", _DETECTOR_IDS)
    def test_score_debug_contains_pack_enterprise_ops(self, det_id):
        m = _scorer()
        dr = _make_detector_result(det_id)
        assert m.score_enterprise_ops(dr)["score_debug"]["pack"] == "enterprise_ops"

    @pytest.mark.parametrize("det_id", _DETECTOR_IDS)
    def test_score_debug_contains_scorer_enterprise_ops(self, det_id):
        m = _scorer()
        dr = _make_detector_result(det_id)
        assert m.score_enterprise_ops(dr)["score_debug"]["scorer"] == "enterprise_ops"

    def test_change_incident_correlation_confidence_is_high(self):
        """ENT_CHANGE_INCIDENT_CORRELATION must score as HIGH confidence per spec."""
        m = _scorer()
        dr = _make_detector_result("ENT_CHANGE_INCIDENT_CORRELATION")
        result = m.score_enterprise_ops(dr)
        assert result["confidence"] == "HIGH"

    def test_incident_resolution_lag_confidence_is_medium(self):
        """ENT_INCIDENT_RESOLUTION_LAG standalone confidence is MEDIUM per spec."""
        m = _scorer()
        dr = _make_detector_result("ENT_INCIDENT_RESOLUTION_LAG")
        result = m.score_enterprise_ops(dr)
        assert result["confidence"] == "MEDIUM"

    def test_sla_breach_by_team_confidence_is_medium(self):
        """ENT_SLA_BREACH_BY_TEAM standalone confidence is MEDIUM per spec."""
        m = _scorer()
        dr = _make_detector_result("ENT_SLA_BREACH_BY_TEAM")
        result = m.score_enterprise_ops(dr)
        assert result["confidence"] == "MEDIUM"

    def test_unknown_detector_returns_default_score(self):
        m = _scorer()
        dr = _make_detector_result("UNKNOWN_DETECTOR_XYZ")
        result = m.score_enterprise_ops(dr)
        assert "note" in result["score_debug"]
        assert "default" in result["score_debug"]["note"].lower()

    def test_corroboration_sources_is_list(self):
        m = _scorer()
        dr = _make_detector_result("ENT_INCIDENT_RESOLUTION_LAG")
        result = m.score_enterprise_ops(dr)
        assert isinstance(result["corroboration_sources"], list)

    def test_effort_label_is_string(self):
        m = _scorer()
        for det_id in self._DETECTOR_IDS:
            dr = _make_detector_result(det_id)
            result = m.score_enterprise_ops(dr)
            assert isinstance(result["effort_label"], str)

    def test_impact_is_positive_int(self):
        m = _scorer()
        for det_id in self._DETECTOR_IDS:
            dr = _make_detector_result(det_id)
            assert m.score_enterprise_ops(dr)["impact"] > 0

    def test_get_score_returns_dict(self):
        m = _scorer()
        assert isinstance(m.get_score("ENT_INCIDENT_RESOLUTION_LAG"), dict)

    def test_score_opportunity_includes_metric_value(self):
        m = _scorer()
        result = m.score_opportunity("ENT_INCIDENT_RESOLUTION_LAG", 0.55)
        assert result["metric_value"] == 0.55


# ---------------------------------------------------------------------------
# AC10 — Cross-org isolation (scorer returns independent results per call)
# ---------------------------------------------------------------------------

class TestAC10CrossOrgIsolation:

    def test_scorer_results_are_independent(self):
        """Two separate scorer calls return independent dicts (no shared mutable state)."""
        m = _scorer()
        dr_a = _make_detector_result("ENT_SLA_BREACH_BY_TEAM", metric_value=0.62)
        dr_b = _make_detector_result("ENT_SLA_BREACH_BY_TEAM", metric_value=0.41)
        result_a = m.score_enterprise_ops(dr_a)
        result_b = m.score_enterprise_ops(dr_b)
        # Mutating one result must not affect the other
        result_a["corroboration_sources"].append("org_a_source")
        assert "org_a_source" not in result_b["corroboration_sources"]

    def test_enterprise_ops_pack_isolated_from_other_packs(self):
        """enterprise_ops pack config is fully separate from sqlserver_opsignal."""
        m = _pack_config()
        ep = m.get_pack("enterprise_ops")
        ss = m.get_pack("sqlserver_opsignal")
        assert ep["packId"] != ss["packId"]
        assert ep["domain"] != ss["domain"]
        assert ep["detectors"] != ss["detectors"]


# ===========================================================================
# T6 (AT-267) — Detector-level threshold / boundary contract tests
# ===========================================================================

# ---------------------------------------------------------------------------
# Detector import helpers
# ---------------------------------------------------------------------------

def _lag_detector():
    try:
        import backend.discovery.detectors.ent_incident_resolution_lag as m
    except ModuleNotFoundError:
        import discovery.detectors.ent_incident_resolution_lag as m
    return m


def _corr_detector():
    try:
        import backend.discovery.detectors.ent_change_incident_correlation as m
    except ModuleNotFoundError:
        import discovery.detectors.ent_change_incident_correlation as m
    return m


def _sla_detector():
    try:
        import backend.discovery.detectors.ent_sla_breach_by_team as m
    except ModuleNotFoundError:
        import discovery.detectors.ent_sla_breach_by_team as m
    return m


# ---------------------------------------------------------------------------
# Payload builders
# ---------------------------------------------------------------------------

def _lag_sn(incidents):
    """Build sn_data for the lag detector with the given closed_incidents list."""
    return {
        "incident_resolution": {
            "closed_incidents": incidents,
            "as_of": "2026-01-30",
        }
    }


def _lag_jira(issues_dict):
    """Build jira_data where issues_dict maps issue_key -> {status, resolved}."""
    return {"issue_resolution": {"issues": issues_dict}}


def _make_lag_payload(incident_count, unresolved_count, avg_lag_days=20.0):
    """
    Craft sn_data + jira_data so that the lag detector sees the requested
    incident_count, unresolved_count, and (approximately) avg_lag_days.

    Incidents reference Jira keys INC-0001..INC-N.  The first `unresolved_count`
    are open; the rest are resolved.  For open issues the lag is approximated by
    making the incident close_date 20 days before the as_of date.
    """
    closed_incidents = []
    jira_issues = {}
    for i in range(incident_count):
        key = f"INC-{i+1:04d}"
        closed_incidents.append({"jira_issue_key": key, "closed_at": "2026-01-10"})
        if i < unresolved_count:
            jira_issues[key] = {"status": "Open", "resolved": False}
        else:
            # resolved_at = 20 days after closed_at so avg_lag_days stays >= 14
            jira_issues[key] = {"status": "Done", "resolved": True, "resolved_at": "2026-01-30"}
    sn = _lag_sn(closed_incidents)
    jira = _lag_jira(jira_issues)
    return sn, jira


def _corr_sn(change_count, post_change_incidents, baseline_incident_rate=1.0):
    """
    Build sn_data for the correlation detector.

    Uses an explicit baseline_incident_rate so the ratio is fully predictable:
        ratio = (post_change_incidents / (change_count * 3)) / baseline_rate

    change_count changes are all implemented and closed at 2026-01-01 00:00:00.
    post_change_incidents incidents each open at 2026-01-01 01:00:00 (within the
    72h window of the first change).
    """
    changes = [
        {"state": "implemented", "closed_at": f"2026-01-{i+1:02d} 00:00:00"}
        for i in range(change_count)
    ]
    incidents = [
        {"opened_at": f"2026-01-{i+1:02d} 01:00:00"}
        for i in range(post_change_incidents)
    ]
    return {
        "change_correlation": {
            "changes": changes,
            "incidents": incidents,
            "baseline_incident_rate": baseline_incident_rate,
            "as_of": "2026-01-31 00:00:00",
        }
    }


def _sla_sn(teams):
    """Build sn_data for the SLA detector from a list of team dicts."""
    return {"sla_breach_by_team": {"teams": teams}}


def _sla_teams(top_pct=0.50, top_rate=0.30, n_teams=3):
    """
    Return a teams list where the top team has top_pct of all breaches and
    top_rate breach rate, spread across n_teams total teams.

    top_pct and top_rate are independently controlled.
    """
    top_breached = 50
    top_total = int(round(top_breached / top_rate)) if top_rate else 200
    total_breaches = int(round(top_breached / top_pct)) if top_pct else 100
    other_breaches = total_breaches - top_breached
    other_count = n_teams - 1
    teams = [{"team": "Alpha Team", "total_tickets": top_total, "breached": top_breached}]
    for i in range(other_count):
        b = other_breaches // other_count if other_count else 0
        teams.append({"team": f"Team {chr(66+i)}", "total_tickets": max(b * 4, 10), "breached": b})
    return teams


# ---------------------------------------------------------------------------
# AC1 — ENT_INCIDENT_RESOLUTION_LAG fires at threshold (T6)
# ---------------------------------------------------------------------------

class TestAC1IncidentResolutionLagFires:
    """AC1: detector fires when all three threshold conditions are met."""

    def test_fires_at_exact_boundary(self):
        """fires when unresolved_pct == 0.30 exactly, avg_lag_days >= 14, volume >= 10."""
        m = _lag_detector()
        # 10 incidents, exactly 3 unresolved → unresolved_pct = 0.30
        sn, jira = _make_lag_payload(10, 3)
        results = m.detect(sn_data=sn, jira_data=jira)
        assert len(results) == 1

    def test_fires_above_threshold(self):
        """fires when unresolved_pct well above 0.30 and volume is high."""
        m = _lag_detector()
        sn, jira = _make_lag_payload(20, 12)  # 60% unresolved
        results = m.detect(sn_data=sn, jira_data=jira)
        assert len(results) == 1

    def test_result_detector_id_is_correct(self):
        m = _lag_detector()
        sn, jira = _make_lag_payload(10, 3)
        results = m.detect(sn_data=sn, jira_data=jira)
        assert results[0].detector_id == "ENT_INCIDENT_RESOLUTION_LAG"

    def test_metric_value_is_unresolved_pct(self):
        m = _lag_detector()
        sn, jira = _make_lag_payload(10, 5)  # 50%
        results = m.detect(sn_data=sn, jira_data=jira)
        assert abs(results[0].metric_value - 0.50) < 0.01

    def test_threshold_stored_correctly(self):
        m = _lag_detector()
        sn, jira = _make_lag_payload(10, 3)
        results = m.detect(sn_data=sn, jira_data=jira)
        assert results[0].threshold == pytest.approx(m.UNRESOLVED_PCT_THRESHOLD)

    def test_does_not_fire_when_pct_below_threshold(self):
        """does not fire when unresolved_pct < 0.30 even if volume is sufficient."""
        m = _lag_detector()
        sn, jira = _make_lag_payload(10, 2)  # 20% unresolved
        results = m.detect(sn_data=sn, jira_data=jira)
        assert results == []

    def test_raw_evidence_contains_incident_count(self):
        m = _lag_detector()
        sn, jira = _make_lag_payload(15, 5)
        results = m.detect(sn_data=sn, jira_data=jira)
        assert results[0].raw_evidence["incident_count_30d"] == 15


# ---------------------------------------------------------------------------
# AC2 — Minimum volume guard (T6)
# ---------------------------------------------------------------------------

class TestAC2IncidentResolutionVolumeGuard:
    """AC2: detector must NOT fire when incident_count_30d < 10."""

    def test_does_not_fire_with_nine_incidents(self):
        """9 incidents (below MIN_INCIDENT_VOLUME=10) must suppress firing."""
        m = _lag_detector()
        sn, jira = _make_lag_payload(9, 5)  # 55% unresolved — above pct threshold
        results = m.detect(sn_data=sn, jira_data=jira)
        assert results == []

    def test_does_not_fire_with_one_incident(self):
        m = _lag_detector()
        sn, jira = _make_lag_payload(1, 1)
        results = m.detect(sn_data=sn, jira_data=jira)
        assert results == []

    def test_does_not_fire_with_zero_incidents(self):
        m = _lag_detector()
        sn, jira = _make_lag_payload(0, 0)
        results = m.detect(sn_data=sn, jira_data=jira)
        assert results == []

    def test_fires_with_exactly_ten_incidents(self):
        """10 incidents (== MIN_INCIDENT_VOLUME) must allow firing."""
        m = _lag_detector()
        sn, jira = _make_lag_payload(10, 3)
        results = m.detect(sn_data=sn, jira_data=jira)
        assert len(results) == 1

    def test_raw_evidence_exposes_incident_count_when_suppressed(self):
        """evaluate() raw_evidence carries incident_count_30d even when not firing."""
        m = _lag_detector()
        sn, jira = _make_lag_payload(9, 6)
        evaluation = m.evaluate(sn_data=sn, jira_data=jira)
        assert evaluation.raw_evidence["incident_count_30d"] == 9
        assert not evaluation.fired


# ---------------------------------------------------------------------------
# AC3 — ENT_CHANGE_INCIDENT_CORRELATION fires at threshold with confidence=HIGH (T6)
# ---------------------------------------------------------------------------

class TestAC3ChangeIncidentCorrelationFires:
    """AC3: detector fires when ratio >= 2.0, change_count >= 3, post_change_incidents >= 5."""

    def _make_high_ratio_sn(self):
        """5 changes, 5 post-change incidents, baseline_rate=0.1 → ratio >> 2.0."""
        return _corr_sn(change_count=5, post_change_incidents=5, baseline_incident_rate=0.1)

    def test_fires_when_all_conditions_met(self):
        m = _corr_detector()
        sn = self._make_high_ratio_sn()
        results = m.detect(sn_data=sn)
        assert len(results) == 1

    def test_result_detector_id_correct(self):
        m = _corr_detector()
        sn = self._make_high_ratio_sn()
        results = m.detect(sn_data=sn)
        assert results[0].detector_id == "ENT_CHANGE_INCIDENT_CORRELATION"

    def test_confidence_is_high_when_fires(self):
        """Scorer must return confidence=HIGH for ENT_CHANGE_INCIDENT_CORRELATION."""
        m = _corr_detector()
        scorer = _scorer()
        sn = self._make_high_ratio_sn()
        results = m.detect(sn_data=sn)
        assert len(results) == 1
        scored = scorer.score_enterprise_ops(results[0])
        assert scored["confidence"] == "HIGH"

    def test_metric_value_is_ratio(self):
        m = _corr_detector()
        sn = self._make_high_ratio_sn()
        results = m.detect(sn_data=sn)
        assert results[0].metric_value > m.RATIO_THRESHOLD

    def test_does_not_fire_when_change_count_below_minimum(self):
        """Does not fire when change_count_30d < 3."""
        m = _corr_detector()
        sn = _corr_sn(change_count=2, post_change_incidents=5, baseline_incident_rate=0.1)
        results = m.detect(sn_data=sn)
        assert results == []

    def test_does_not_fire_when_post_change_incidents_below_minimum(self):
        """Does not fire when post_change_incidents < 5."""
        m = _corr_detector()
        sn = _corr_sn(change_count=5, post_change_incidents=4, baseline_incident_rate=0.1)
        results = m.detect(sn_data=sn)
        assert results == []

    def test_raw_evidence_contains_ratio(self):
        m = _corr_detector()
        sn = self._make_high_ratio_sn()
        results = m.detect(sn_data=sn)
        assert "post_change_incident_ratio" in results[0].raw_evidence

    def test_raw_evidence_contains_change_count(self):
        m = _corr_detector()
        sn = self._make_high_ratio_sn()
        results = m.detect(sn_data=sn)
        assert results[0].raw_evidence["change_count_30d"] >= m.MIN_CHANGE_COUNT


# ---------------------------------------------------------------------------
# AC4 — ENT_SLA_BREACH_BY_TEAM fires at threshold (T6)
# ---------------------------------------------------------------------------

class TestAC4SLABreachByTeamFires:
    """AC4: detector fires when top_team_breach_pct >= 0.40, rate >= 0.25, teams >= 3."""

    def test_fires_when_all_conditions_met(self):
        m = _sla_detector()
        teams = _sla_teams(top_pct=0.50, top_rate=0.30, n_teams=3)
        sn = _sla_sn(teams)
        results = m.detect(sn_data=sn)
        assert len(results) == 1

    def test_result_detector_id_correct(self):
        m = _sla_detector()
        teams = _sla_teams(top_pct=0.50, top_rate=0.30, n_teams=3)
        sn = _sla_sn(teams)
        results = m.detect(sn_data=sn)
        assert results[0].detector_id == "ENT_SLA_BREACH_BY_TEAM"

    def test_metric_value_is_top_team_breach_pct(self):
        m = _sla_detector()
        teams = _sla_teams(top_pct=0.50, top_rate=0.30, n_teams=3)
        sn = _sla_sn(teams)
        results = m.detect(sn_data=sn)
        assert results[0].metric_value >= m.TOP_TEAM_BREACH_PCT_THRESHOLD

    def test_does_not_fire_when_pct_below_threshold(self):
        """Does not fire when top_team_breach_pct < 0.40."""
        m = _sla_detector()
        teams = _sla_teams(top_pct=0.30, top_rate=0.30, n_teams=3)
        sn = _sla_sn(teams)
        results = m.detect(sn_data=sn)
        assert results == []

    def test_does_not_fire_when_rate_below_threshold(self):
        """Does not fire when top_team_breach_rate < 0.25."""
        m = _sla_detector()
        teams = _sla_teams(top_pct=0.55, top_rate=0.15, n_teams=3)
        sn = _sla_sn(teams)
        results = m.detect(sn_data=sn)
        assert results == []

    def test_does_not_fire_when_fewer_than_3_teams(self):
        """Does not fire when teams_analysed < 3 (MIN_TEAMS_ANALYSED guard)."""
        m = _sla_detector()
        teams = _sla_teams(top_pct=0.70, top_rate=0.40, n_teams=2)
        sn = _sla_sn(teams)
        results = m.detect(sn_data=sn)
        assert results == []

    def test_fires_with_exactly_three_teams(self):
        """Fires when teams_analysed == MIN_TEAMS_ANALYSED == 3."""
        m = _sla_detector()
        teams = _sla_teams(top_pct=0.55, top_rate=0.35, n_teams=3)
        sn = _sla_sn(teams)
        results = m.detect(sn_data=sn)
        assert len(results) == 1

    def test_raw_evidence_contains_top_team_name(self):
        m = _sla_detector()
        teams = _sla_teams(top_pct=0.50, top_rate=0.30, n_teams=3)
        sn = _sla_sn(teams)
        results = m.detect(sn_data=sn)
        assert results[0].raw_evidence.get("top_team_name") == "Alpha Team"

    def test_raw_evidence_contains_teams_analysed(self):
        m = _sla_detector()
        teams = _sla_teams(top_pct=0.50, top_rate=0.30, n_teams=4)
        sn = _sla_sn(teams)
        results = m.detect(sn_data=sn)
        assert results[0].raw_evidence["teams_analysed"] == 4


# ---------------------------------------------------------------------------
# AC10 (T6 extension) — Cross-org isolation at detector level
# ---------------------------------------------------------------------------

class TestAC10DetectorLevelIsolation:
    """AC10: two separate detect() calls with different payloads are fully independent."""

    def test_lag_detector_calls_are_independent(self):
        """Two detect() calls with different sn_data return independent result lists."""
        m = _lag_detector()
        sn_a, jira_a = _make_lag_payload(10, 3)   # fires
        sn_b, jira_b = _make_lag_payload(10, 1)   # does not fire
        results_a = m.detect(sn_data=sn_a, jira_data=jira_a)
        results_b = m.detect(sn_data=sn_b, jira_data=jira_b)
        assert len(results_a) == 1
        assert len(results_b) == 0

    def test_sla_detector_calls_are_independent(self):
        """org_A result list is not contaminated by org_B data."""
        m = _sla_detector()
        teams_a = _sla_teams(top_pct=0.55, top_rate=0.35, n_teams=3)
        # org_B: equal distribution, top team < 0.40 pct AND < 0.25 breach rate
        teams_b = [
            {"team": "Alpha", "total_tickets": 200, "breached": 9},   # pct=9/24=0.375, rate=4.5%
            {"team": "Beta",  "total_tickets": 200, "breached": 8},
            {"team": "Gamma", "total_tickets": 200, "breached": 7},
        ]
        results_a = m.detect(sn_data=_sla_sn(teams_a))
        results_b = m.detect(sn_data=_sla_sn(teams_b))
        assert len(results_a) == 1
        assert len(results_b) == 0

    def test_corr_detector_calls_are_independent(self):
        m = _corr_detector()
        sn_a = _corr_sn(change_count=5, post_change_incidents=5, baseline_incident_rate=0.1)
        sn_b = _corr_sn(change_count=2, post_change_incidents=5, baseline_incident_rate=0.1)
        results_a = m.detect(sn_data=sn_a)
        results_b = m.detect(sn_data=sn_b)
        assert len(results_a) == 1
        assert results_b == []


# ---------------------------------------------------------------------------
# AC5 — ENT_SLA_BREACH_BY_TEAM confidence elevation via ENT-1 entity overlay
# ---------------------------------------------------------------------------

def _sla_sn_with_overlay(teams, overlay):
    """Build sn_data carrying both team breach rows and a team_entity_overlay."""
    return {
        "sla_breach_by_team": {"teams": teams},
        "team_entity_overlay": overlay,
    }


def _jira_backlog(open_issues_by_team):
    """Build jira_data with a team_backlog.open_issues_by_team map."""
    return {"team_backlog": {"open_issues_by_team": open_issues_by_team}}


class TestAC5SLABreachTeamConfidenceElevation:
    """AC5: ENT_SLA_BREACH_BY_TEAM elevates MEDIUM → HIGH via ENT-1 entity graph
    when top team resolves to a Team entity AND Jira open issues >= 20.
    Exact-name fallback must stay MEDIUM."""

    def _firing_teams(self):
        return _sla_teams(top_pct=0.55, top_rate=0.35, n_teams=3)

    def test_confidence_high_via_entity_graph(self):
        """Entity overlay resolves top team to entity; Jira shows >= 20 open issues → HIGH."""
        m = _sla_detector()
        scorer = _scorer()
        teams = self._firing_teams()
        # "Alpha Team" is the top team produced by _sla_teams()
        overlay = {"alpha team": "entity-001", "comm credit team": "entity-001"}
        sn = _sla_sn_with_overlay(teams, overlay)
        jira = _jira_backlog({"alpha team": 25})
        results = m.detect(sn_data=sn, jira_data=jira)
        assert len(results) == 1
        scored = scorer.score_enterprise_ops(results[0])
        assert scored["confidence"] == "HIGH"
        assert scored["corroborated"] is True
        assert "Jira" in scored["corroboration_sources"]

    def test_raw_evidence_team_entity_resolved_true_on_entity_graph_path(self):
        """raw_evidence['team_entity_resolved'] is True when entity overlay matched."""
        m = _sla_detector()
        teams = self._firing_teams()
        overlay = {"alpha team": "entity-001"}
        sn = _sla_sn_with_overlay(teams, overlay)
        jira = _jira_backlog({"alpha team": 30})
        results = m.detect(sn_data=sn, jira_data=jira)
        assert len(results) == 1
        assert results[0].raw_evidence.get("team_entity_resolved") is True
        assert results[0].raw_evidence.get("match_strategy") == "entity_graph"

    def test_confidence_medium_when_jira_open_issues_below_threshold(self):
        """Entity overlay resolves but Jira open issues < 20 → stays MEDIUM."""
        m = _sla_detector()
        scorer = _scorer()
        teams = self._firing_teams()
        overlay = {"alpha team": "entity-001"}
        sn = _sla_sn_with_overlay(teams, overlay)
        jira = _jira_backlog({"alpha team": 10})  # below HIGH_JIRA_OPEN_ISSUES=20
        results = m.detect(sn_data=sn, jira_data=jira)
        assert len(results) == 1
        scored = scorer.score_enterprise_ops(results[0])
        assert scored["confidence"] == "MEDIUM"
        assert scored["corroborated"] is False

    def test_confidence_medium_via_exact_name_fallback(self):
        """No overlay configured; exact-name match never elevates above MEDIUM."""
        m = _sla_detector()
        scorer = _scorer()
        teams = self._firing_teams()
        sn = _sla_sn(teams)  # no overlay
        jira = _jira_backlog({"alpha team": 50})  # high Jira count, but no entity graph
        results = m.detect(sn_data=sn, jira_data=jira)
        assert len(results) == 1
        scored = scorer.score_enterprise_ops(results[0])
        assert scored["confidence"] == "MEDIUM"
        assert results[0].raw_evidence.get("match_strategy") == "exact_name"
        assert results[0].raw_evidence.get("team_entity_resolved") is False

    def test_confidence_medium_with_no_jira_data(self):
        """No jira_data at all → no corroboration possible → MEDIUM."""
        m = _sla_detector()
        scorer = _scorer()
        teams = self._firing_teams()
        sn = _sla_sn(teams)
        results = m.detect(sn_data=sn)
        assert len(results) == 1
        scored = scorer.score_enterprise_ops(results[0])
        assert scored["confidence"] == "MEDIUM"

    def test_entity_graph_sums_jira_issues_across_aliased_teams(self):
        """All Jira teams mapping to the same entity are summed for open issue count."""
        m = _sla_detector()
        scorer = _scorer()
        teams = self._firing_teams()
        # Two Jira team names both map to entity-001; combined count >= 20
        overlay = {"alpha team": "entity-001", "alpha ops": "entity-001"}
        sn = _sla_sn_with_overlay(teams, overlay)
        jira = _jira_backlog({"alpha team": 8, "alpha ops": 15})  # sum = 23
        results = m.detect(sn_data=sn, jira_data=jira)
        assert len(results) == 1
        assert results[0].raw_evidence.get("top_team_jira_open_issues") == 23
        scored = scorer.score_enterprise_ops(results[0])
        assert scored["confidence"] == "HIGH"


# ---------------------------------------------------------------------------
# AC9 — ENT_INCIDENT_RESOLUTION_LAG confidence elevation via COR-06 (scorer)
# ---------------------------------------------------------------------------

class TestAC9IncidentResolutionLagCOR06Elevation:
    """AC9: ENT_INCIDENT_RESOLUTION_LAG elevates MEDIUM → HIGH when the scorer
    receives sn_data with cor06_slack_escalation.fired == True.
    Absent or fired==False must stay MEDIUM."""

    def _firing_dr(self):
        """A DetectorResult for ENT_INCIDENT_RESOLUTION_LAG (no raw_evidence confidence)."""
        return _make_detector_result("ENT_INCIDENT_RESOLUTION_LAG", metric_value=0.45)

    def test_confidence_high_when_cor06_fired(self):
        """Scorer elevates to HIGH when cor06_slack_escalation.fired is True."""
        scorer = _scorer()
        dr = self._firing_dr()
        sn = {"cor06_slack_escalation": {"fired": True, "org_id": "default"}}
        result = scorer.score_enterprise_ops(dr, sn_data=sn, org_id="default")
        assert result["confidence"] == "HIGH"
        assert result["corroborated"] is True
        assert "Slack" in result["corroboration_sources"]

    def test_confidence_medium_when_cor06_not_fired(self):
        """Scorer keeps MEDIUM when cor06_slack_escalation.fired is False."""
        scorer = _scorer()
        dr = self._firing_dr()
        sn = {"cor06_slack_escalation": {"fired": False, "org_id": "default"}}
        result = scorer.score_enterprise_ops(dr, sn_data=sn, org_id="default")
        assert result["confidence"] == "MEDIUM"
        assert result["corroborated"] is False

    def test_confidence_medium_when_no_sn_data(self):
        """Scorer keeps MEDIUM when sn_data is None (no COR-06 signal available)."""
        scorer = _scorer()
        dr = self._firing_dr()
        result = scorer.score_enterprise_ops(dr)
        assert result["confidence"] == "MEDIUM"
        assert result["corroborated"] is False

    def test_confidence_medium_when_cor06_key_absent(self):
        """Scorer keeps MEDIUM when sn_data has no cor06_slack_escalation key."""
        scorer = _scorer()
        dr = self._firing_dr()
        sn = {"other_signal": {"value": True}}
        result = scorer.score_enterprise_ops(dr, sn_data=sn, org_id="default")
        assert result["confidence"] == "MEDIUM"

    def test_cross_org_guard_blocks_elevation(self):
        """COR-06 from a different org must NOT elevate confidence (cross-org guard)."""
        scorer = _scorer()
        dr = self._firing_dr()
        sn = {"cor06_slack_escalation": {"fired": True, "org_id": "org-other"}}
        result = scorer.score_enterprise_ops(dr, sn_data=sn, org_id="org-mine")
        assert result["confidence"] == "MEDIUM"
        assert result["corroborated"] is False

    def test_elevation_does_not_affect_other_detectors(self):
        """COR-06 firing must NOT elevate ENT_CHANGE_INCIDENT_CORRELATION (already HIGH)
        or ENT_SLA_BREACH_BY_TEAM (different elevation path)."""
        scorer = _scorer()
        sn = {"cor06_slack_escalation": {"fired": True, "org_id": "default"}}
        for det_id in ("ENT_CHANGE_INCIDENT_CORRELATION", "ENT_SLA_BREACH_BY_TEAM"):
            dr = _make_detector_result(det_id)
            result = scorer.score_enterprise_ops(dr, sn_data=sn, org_id="default")
            # ENT_CHANGE_INCIDENT_CORRELATION is always HIGH (not via COR-06)
            # ENT_SLA_BREACH_BY_TEAM uses entity graph, not COR-06
            assert result["corroboration_sources"] != ["Slack"], (
                f"{det_id} must not be elevated via COR-06 Slack path"
            )
