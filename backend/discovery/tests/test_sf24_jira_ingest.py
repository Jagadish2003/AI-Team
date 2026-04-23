"""
SF-2.4 tests — Jira ingestion module.
All tests run in offline mode. No Jira credentials required.
"""
from __future__ import annotations
import os
import pytest

os.environ["INGEST_MODE"] = "offline"


@pytest.fixture
def jira_data():
    from discovery.ingest.jira import ingest
    return ingest()


# ── Shape tests ───────────────────────────────────────────────────────────────

class TestIngestShape:
    def test_top_level_keys(self, jira_data):
        assert "issue_metrics" in jira_data
        assert "sprint_velocity" in jira_data

    def test_issue_metrics_shape(self, jira_data):
        im = jira_data["issue_metrics"]
        assert isinstance(im["total_issues_90d"], int)
        assert isinstance(im["salesforce_label_count"], int)
        assert isinstance(im["jira_echo_score"], float)
        assert isinstance(im["issue_type_breakdown"], list)
        assert isinstance(im["sample_cross_references"], list)

    def test_sprint_velocity_shape(self, jira_data):
        sv = jira_data["sprint_velocity"]
        assert isinstance(sv, list)
        assert len(sv) >= 1
        for sprint in sv:
            assert "sprint_name" in sprint
            assert isinstance(sprint["completed_points"], (int, float))
            assert isinstance(sprint["salesforce_issue_count"], int)

    def test_echo_score_derived_correctly(self, jira_data):
        """jira_echo_score = salesforce_label_count / total_issues_90d"""
        im = jira_data["issue_metrics"]
        if im["total_issues_90d"] > 0:
            expected = round(im["salesforce_label_count"] / im["total_issues_90d"], 4)
            assert abs(im["jira_echo_score"] - expected) < 0.01


# ── D7 readiness tests ────────────────────────────────────────────────────────

class TestD7Readiness:
    def test_jira_echo_score_fires_d7(self, jira_data):
        """D7: jira_echo_score > 0.15"""
        score = jira_data["issue_metrics"]["jira_echo_score"]
        assert score > 0.15, f"D7 will not fire from Jira side: jira_echo_score={score}"

    def test_sample_cross_references_present(self, jira_data):
        refs = jira_data["issue_metrics"]["sample_cross_references"]
        assert len(refs) >= 1
        for r in refs:
            assert "issue_key" in r
            assert "summary" in r


# ── Fix verification tests ────────────────────────────────────────────────────

class TestKnownFixesApplied:
    def test_completed_points_not_none(self, jira_data):
        """Fix: completed_points was None in earlier stub."""
        for sprint in jira_data["sprint_velocity"]:
            assert sprint["completed_points"] is not None, \
                f"completed_points is None in sprint {sprint['sprint_name']}"
            assert isinstance(sprint["completed_points"], (int, float))

    def test_salesforce_issue_count_not_none(self, jira_data):
        """Fix: salesforce_issue_count was None in earlier stub."""
        for sprint in jira_data["sprint_velocity"]:
            assert sprint["salesforce_issue_count"] is not None, \
                f"salesforce_issue_count is None in sprint {sprint['sprint_name']}"

    def test_sprint_metrics_present(self, jira_data):
        """Verify that all required sprint metrics are present in the output."""
        for sprint in jira_data["sprint_velocity"]:
            # Assert completed_points exists and is a number
            assert "completed_points" in sprint, f"Missing completed_points in {sprint.get('sprint_name')}"
            assert isinstance(sprint["completed_points"], (int, float)), "completed_points must be a number"

            # Assert salesforce_issue_count exists and is a number
            assert "salesforce_issue_count" in sprint, f"Missing salesforce_issue_count in {sprint.get('sprint_name')}"
            assert isinstance(sprint["salesforce_issue_count"], (int, float)), "salesforce_issue_count must be a number"

            # Assert velocity_trend exists and is a string
            assert "velocity_trend" in sprint, f"Missing velocity_trend in {sprint.get('sprint_name')}"
            assert isinstance(sprint["velocity_trend"], str), "velocity_trend must be a string"


# ── Individual function tests ─────────────────────────────────────────────────

class TestIndividualFunctions:
    def test_get_issue_metrics_offline(self):
        from discovery.ingest.jira import get_issue_metrics
        result = get_issue_metrics()
        assert result["total_issues_90d"] == 280
        assert result["salesforce_label_count"] == 62
        assert result["jira_echo_score"] == 0.22

    def test_get_sprint_velocity_offline(self):
        from discovery.ingest.jira import get_sprint_velocity
        result = get_sprint_velocity()
        assert len(result) == 3
        assert result[0]["sprint_name"] == "CRM Sprint 14"
        assert result[0]["completed_points"] == 42
        assert result[0]["salesforce_issue_count"] == 18

    def test_extract_story_points(self):
        from discovery.ingest.jira import _extract_story_points
        assert _extract_story_points({"fields": {"customfield_10016": 5.0}}) == 5.0
        assert _extract_story_points({"fields": {"customfield_10002": 3}}) == 3.0
        assert _extract_story_points({"fields": {}}) is None
        assert _extract_story_points({"fields": {"customfield_10016": None}}) is None

    def test_extract_sf_case_id(self):
        from discovery.ingest.jira import _extract_sf_case_id
        assert _extract_sf_case_id("Fix account sync — see Salesforce case CS-1023") == "CS-1023"
        assert _extract_sf_case_id("No case reference here") == ""
        assert _extract_sf_case_id("CS-9999 is the ticket") == "CS-9999"

    def test_is_salesforce_related_by_label(self):
        from discovery.ingest.jira import _is_salesforce_related
        issue = {"fields": {"labels": [{"name": "Salesforce"}], "summary": "some task"}}
        assert _is_salesforce_related(issue) is True

    def test_is_salesforce_related_by_summary(self):
        from discovery.ingest.jira import _is_salesforce_related
        issue = {"fields": {"labels": [], "summary": "Fix CS-1234 integration bug"}}
        assert _is_salesforce_related(issue) is True

    def test_is_not_salesforce_related(self):
        from discovery.ingest.jira import _is_salesforce_related
        issue = {"fields": {"labels": [{"name": "backend"}], "summary": "Refactor DB layer"}}
        assert _is_salesforce_related(issue) is False


# ── Error handling tests ──────────────────────────────────────────────────────

class TestErrorHandling:
    def test_missing_fixture_raises(self, tmp_path, monkeypatch):
        from discovery.ingest import jira as jira_mod
        monkeypatch.setattr(jira_mod, "FIXTURE_PATH", tmp_path / "missing.json")
        with pytest.raises(jira_mod.JiraIngestError, match="fixture not found"):
            jira_mod.ingest()

    def test_live_no_url_returns_empty(self, monkeypatch):
        """If JIRA_URL not set in live mode, ingest() returns {} gracefully."""
        monkeypatch.setenv("INGEST_MODE", "live")
        monkeypatch.delenv("JIRA_URL", raising=False)
        import importlib
        import discovery.ingest as pkg
        import discovery.ingest.jira as jira_mod
        importlib.reload(pkg)
        importlib.reload(jira_mod)
        result = jira_mod.ingest()
        assert result == {}

    def test_live_no_token_raises(self, monkeypatch):
        """If JIRA_URL set but no token, _get_client raises JiraIngestError."""
        monkeypatch.setenv("INGEST_MODE", "live")
        monkeypatch.setenv("JIRA_URL", "https://test.atlassian.net")
        monkeypatch.delenv("JIRA_TOKEN", raising=False)
        monkeypatch.delenv("JIRA_USER", raising=False)
        import importlib
        import discovery.ingest as pkg
        import discovery.ingest.jira as jira_mod
        importlib.reload(pkg)
        importlib.reload(jira_mod)
        with pytest.raises(jira_mod.JiraIngestError, match="JIRA_TOKEN"):
            jira_mod._get_client()
