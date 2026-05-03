"""
ENG-AIQ-NC-2 + ENG-AIQ-NC-3 — Jira + ServiceNow Lending Correlation Tests
Sprint 5 — Wave 2

Tests:
  Jira:
    1. All 5 lending keywords groups produce matches
    2. Issues with correct labels map to correct detector
    3. Non-lending issues produce no match
    4. Snippet is banking-language (not raw API text)
    5. by_detector groups correctly
    6. Empty input returns empty result
    7. Offline fixture produces lending_correlation
    8. All 5 seed issues match expected detectors

  ServiceNow:
    1-8 same pattern as Jira
    9. SN short_description used as primary match field
    10. category field also searched

Run:
  pytest tests/contract/test_lending_correlation.py -v
"""
from __future__ import annotations

import json
import os
import pytest

from discovery.ingest.jira import (
    get_lending_correlation as jira_lending,
    _detector_for_issue,
    _build_lending_snippet,
    LENDING_KEYWORD_MAP,
)
from discovery.ingest.servicenow import (
    get_lending_correlation as sn_lending,
    _sn_detector_for_incident,
    _sn_build_lending_snippet,
    SN_LENDING_KEYWORD_MAP,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

def jira_issue(summary="", labels=None, description="", priority="High"):
    return {
        "id": "TEST-001", "key": "TEST-001",
        "summary": summary,
        "description": description,
        "labels": labels or [],
        "priority": priority,
        "status": "Open",
        "project": "LOAN",
    }

def sn_incident(short_desc="", description="", category="", priority="2 - High"):
    return {
        "id": "INC001", "number": "INC001",
        "short_description": short_desc,
        "description": description,
        "category": category,
        "subcategory": "",
        "priority": priority,
        "state": "New",
    }


# ── Jira: detector matching ───────────────────────────────────────────────────

class TestJiraDetectorMatching:

    def test_routing_keywords_match_routing_detector(self):
        issue = jira_issue(summary="Underwriting assignment broken for CRE loans",
                           labels=["loan","underwriting","routing"])
        result = _detector_for_issue(issue)
        assert result is not None
        assert result[0] == "LOAN_ORIGINATION_ROUTING_FRICTION"

    def test_covenant_keywords_match_covenant_detector(self):
        issue = jira_issue(summary="Covenant review reminders not triggering",
                           labels=["covenant","compliance","exception"])
        result = _detector_for_issue(issue)
        assert result is not None
        assert result[0] == "COVENANT_TRACKING_GAP"

    def test_checklist_keywords_match_checklist_detector(self):
        issue = jira_issue(summary="Pre-close checklist delays blocking closings",
                           labels=["closing","loan"])
        result = _detector_for_issue(issue)
        assert result is not None
        assert result[0] == "CHECKLIST_BOTTLENECK"

    def test_spreading_keywords_match_spreading_detector(self):
        issue = jira_issue(summary="Credit analyst spreading queue behind",
                           labels=["spreading","credit-review"])
        result = _detector_for_issue(issue)
        assert result is not None
        assert result[0] == "SPREADING_BOTTLENECK"

    def test_approval_keywords_match_approval_detector(self):
        issue = jira_issue(summary="Loan approval stuck — no notifications",
                           labels=["loan","approval"])
        result = _detector_for_issue(issue)
        assert result is not None
        assert result[0] == "APPROVAL_BOTTLENECK"

    def test_non_lending_issue_returns_none(self):
        issue = jira_issue(summary="Update CI pipeline configuration",
                           labels=["devops","ci"])
        result = _detector_for_issue(issue)
        assert result is None

    def test_generic_approval_keyword_alone_does_not_fire(self):
        """Single 'approval' in summary without lending context should not fire."""
        issue = jira_issue(summary="Approval workflow configuration update",
                           labels=["workflow","admin"])
        result = _detector_for_issue(issue)
        assert result is None, "Generic 'approval' alone should not fire APPROVAL_BOTTLENECK"

    def test_label_match_fires_without_summary(self):
        """Label match alone (score=2.0) is sufficient to fire."""
        issue = jira_issue(summary="Issue with process",
                           labels=["covenant","compliance"])
        result = _detector_for_issue(issue)
        assert result is not None
        assert result[0] == "COVENANT_TRACKING_GAP"

    def test_description_contributes_to_score(self):
        """Description alone (0.5) does not fire. Summary + description (1.5) does."""
        # Description only — should NOT fire (score = 0.5, below threshold of 1.5)
        issue_desc_only = jira_issue(
            summary="Process improvement needed",
            description="The covenant review process is not triggering alerts."
        )
        result_desc_only = _detector_for_issue(issue_desc_only)
        assert result_desc_only is None, "Description-only match should not fire (score < 1.5)"

        # Summary + description — should fire (score = 1.5)
        issue_both = jira_issue(
            summary="Covenant review not working",
            description="The compliance alerts are not triggering correctly."
        )
        result_both = _detector_for_issue(issue_both)
        assert result_both is not None
        assert result_both[0] == "COVENANT_TRACKING_GAP"


# ── Jira: snippet quality ─────────────────────────────────────────────────────

class TestJiraSnippetQuality:

    def test_snippet_contains_banking_label(self):
        issue = jira_issue(summary="Underwriting assignment broken")
        snippet = _build_lending_snippet(issue, "Loan origination routing")
        assert "Loan origination routing" in snippet

    def test_snippet_contains_summary(self):
        issue = jira_issue(summary="Covenant review not working")
        snippet = _build_lending_snippet(issue, "Covenant compliance")
        assert "Covenant review not working" in snippet

    def test_snippet_contains_priority(self):
        issue = jira_issue(summary="test", priority="High")
        snippet = _build_lending_snippet(issue, "Test")
        assert "High" in snippet


# ── Jira: get_lending_correlation ─────────────────────────────────────────────

class TestJiraLendingCorrelation:

    def test_empty_issues_returns_empty(self):
        result = jira_lending(fixture_issues=[])
        assert result["total_matched"] == 0
        assert result["lending_issues"] == []
        assert result["by_detector"] == {}

    def test_five_seed_issues_all_match(self):
        issues = [
            jira_issue("Underwriting assignment broken",["loan","underwriting","routing"]),
            jira_issue("Covenant review reminders not triggering",["covenant","compliance"]),
            jira_issue("Checklist delays blocking closings",["closing","loan"]),
            jira_issue("Spreading queue behind",["spreading","credit-review"]),
            jira_issue("Loan approval stuck",["loan","approval"]),
        ]
        result = jira_lending(fixture_issues=issues)
        assert result["total_matched"] == 5

    def test_by_detector_groups_correctly(self):
        issues = [
            jira_issue("Covenant issue 1", ["covenant","compliance"]),
            jira_issue("Covenant issue 2", ["covenant","exception"]),
            jira_issue("Checklist issue",  ["closing","loan"]),
        ]
        result = jira_lending(fixture_issues=issues)
        assert len(result["by_detector"]["COVENANT_TRACKING_GAP"]) == 2
        assert len(result["by_detector"]["CHECKLIST_BOTTLENECK"]) == 1

    def test_each_lending_issue_has_required_keys(self):
        issues = [jira_issue("Covenant review broken", ["covenant"])]
        result = jira_lending(fixture_issues=issues)
        item = result["lending_issues"][0]
        assert "issue_id" in item
        assert "detector_id" in item
        assert "snippet" in item
        assert "source" in item
        assert item["source"] == "Jira"
        assert "detectorId" in item

    def test_detectorId_matches_detector_id(self):
        """detectorId must match detector_id for evidence_builder compatibility."""
        issues = [jira_issue("Spreading queue behind", ["spreading"])]
        result = jira_lending(fixture_issues=issues)
        item = result["lending_issues"][0]
        assert item["detectorId"] == item["detector_id"]

    def test_offline_fixture_produces_lending_correlation(self):
        """Offline ingest() includes lending_correlation key."""
        import os
        env = {k: v for k, v in os.environ.items()}
        env.pop("INGEST_MODE", None)
        os.environ["INGEST_MODE"] = "offline"
        try:
            from discovery.ingest.jira import ingest
            result = ingest()
            assert "lending_correlation" in result
            assert result["lending_correlation"]["total_matched"] >= 0
        finally:
            if "INGEST_MODE" in env:
                os.environ["INGEST_MODE"] = env["INGEST_MODE"]
            else:
                os.environ.pop("INGEST_MODE", None)

    def test_non_lending_issues_filtered_out(self):
        issues = [
            jira_issue("Update CI pipeline", ["devops","ci"]),
            jira_issue("Fix login bug", ["auth","security"]),
            jira_issue("Covenant review broken", ["covenant"]),
        ]
        result = jira_lending(fixture_issues=issues)
        assert result["total_matched"] == 1


# ── ServiceNow: detector matching ─────────────────────────────────────────────

class TestSNDetectorMatching:

    def test_covenant_short_desc_matches_covenant_detector(self):
        inc = sn_incident(short_desc="Compliance team cannot update covenant status")
        result = _sn_detector_for_incident(inc)
        assert result is not None
        assert result[0] == "COVENANT_TRACKING_GAP"

    def test_checklist_short_desc_matches_checklist_detector(self):
        inc = sn_incident(short_desc="Document exception workflow not routing for loan closings")
        result = _sn_detector_for_incident(inc)
        assert result is not None
        assert result[0] == "CHECKLIST_BOTTLENECK"

    def test_spreading_short_desc_matches_spreading_detector(self):
        inc = sn_incident(short_desc="nCino spreading module slow — analysts reporting delays")
        result = _sn_detector_for_incident(inc)
        assert result is not None
        assert result[0] == "SPREADING_BOTTLENECK"

    def test_approval_short_desc_matches_approval_detector(self):
        inc = sn_incident(short_desc="Loan approval stuck — no approval notifications sent")
        result = _sn_detector_for_incident(inc)
        assert result is not None
        assert result[0] == "APPROVAL_BOTTLENECK"

    def test_routing_short_desc_matches_routing_detector(self):
        inc = sn_incident(short_desc="nCino loan routing — multiple reassignments causing delays")
        result = _sn_detector_for_incident(inc)
        assert result is not None
        assert result[0] == "LOAN_ORIGINATION_ROUTING_FRICTION"

    def test_category_field_also_searched(self):
        inc = sn_incident(short_desc="System issue", category="compliance")
        result = _sn_detector_for_incident(inc)
        assert result is not None
        assert result[0] == "COVENANT_TRACKING_GAP"

    def test_non_lending_incident_returns_none(self):
        inc = sn_incident(short_desc="Network switch firmware update required")
        result = _sn_detector_for_incident(inc)
        assert result is None

    def test_description_also_searched(self):
        inc = sn_incident(short_desc="System performance issue",
                          description="The spreading module is running slowly causing analyst backlog.")
        result = _sn_detector_for_incident(inc)
        assert result is not None
        assert result[0] == "SPREADING_BOTTLENECK"


# ── ServiceNow: get_lending_correlation ───────────────────────────────────────

class TestSNLendingCorrelation:

    def test_empty_incidents_returns_empty(self):
        result = sn_lending(fixture_incidents=[])
        assert result["total_matched"] == 0
        assert result["lending_incidents"] == []

    def test_five_seed_incidents_all_match(self):
        incidents = [
            sn_incident("Compliance team cannot update covenant status"),
            sn_incident("Document exception workflow not routing for loan closings"),
            sn_incident("nCino spreading module slow — analysts reporting delays"),
            sn_incident("Loan approval stuck — no notifications sent"),
            sn_incident("nCino loan routing — multiple reassignments"),
        ]
        result = sn_lending(fixture_incidents=incidents)
        assert result["total_matched"] == 5

    def test_by_detector_groups_correctly(self):
        incidents = [
            sn_incident("Covenant issue 1 compliance"),
            sn_incident("Covenant issue 2 breach"),
            sn_incident("Spreading module slow"),
        ]
        result = sn_lending(fixture_incidents=incidents)
        assert len(result["by_detector"]["COVENANT_TRACKING_GAP"]) == 2
        assert len(result["by_detector"]["SPREADING_BOTTLENECK"]) == 1

    def test_each_incident_has_required_keys(self):
        incidents = [sn_incident("Covenant review compliance")]
        result = sn_lending(fixture_incidents=incidents)
        item = result["lending_incidents"][0]
        assert "incident_id" in item
        assert "detector_id" in item
        assert "snippet" in item
        assert item["source"] == "ServiceNow"
        assert "detectorId" in item

    def test_detectorId_matches_detector_id(self):
        incidents = [sn_incident("Loan approval stuck")]
        result = sn_lending(fixture_incidents=incidents)
        item = result["lending_incidents"][0]
        assert item["detectorId"] == item["detector_id"]

    def test_snippet_contains_banking_label(self):
        incidents = [sn_incident("Loan approval process stuck", priority="1 - Critical")]
        result = sn_lending(fixture_incidents=incidents)
        snippet = result["lending_incidents"][0]["snippet"]
        assert "Loan approval" in snippet

    def test_offline_fixture_produces_lending_correlation(self):
        import os
        os.environ["INGEST_MODE"] = "offline"
        try:
            from discovery.ingest.servicenow import ingest
            result = ingest()
            assert "lending_correlation" in result
            assert result["lending_correlation"]["total_matched"] >= 0
        finally:
            os.environ.pop("INGEST_MODE", None)

    def test_non_lending_incidents_filtered_out(self):
        incidents = [
            sn_incident("Network switch firmware update"),
            sn_incident("Printer offline in building 3"),
            sn_incident("Loan approval stuck"),
        ]
        result = sn_lending(fixture_incidents=incidents)
        assert result["total_matched"] == 1
