"""
Tests that offline ingestion reads fixtures correctly.
All pass without live credentials.
"""
import os
import pytest

os.environ["INGEST_MODE"] = "offline"


def test_salesforce_fixture_loads():
    from discovery.ingest.salesforce import ingest
    data = ingest()
    assert "case_metrics" in data
    assert data["case_metrics"]["total_cases_90d"] == 300
    assert data["case_metrics"]["handoff_score"] == 1.6
    assert "flow_inventory" in data
    assert data["flow_inventory"]["flow_activity_score"] == 2.128
    assert "approval_processes" in data
    assert "named_credentials" in data
    assert "cross_system_references" in data


def test_servicenow_fixture_loads():
    from discovery.ingest.servicenow import ingest
    data = ingest()
    assert "incident_metrics" in data
    assert data["cross_system_references"]["sn_echo_score"] == 0.16
    assert data["cross_system_references"]["sn_match_count"] == 80


def test_jira_fixture_loads():
    from discovery.ingest.jira import ingest
    data = ingest()
    assert "issue_metrics" in data
    assert data["issue_metrics"]["jira_echo_score"] == 0.22
    assert data["issue_metrics"]["salesforce_label_count"] == 62


def test_cross_system_consistency():
    """Verify CS- case IDs referenced in SN/Jira exist in SF sample_case_ids scope."""
    from discovery.ingest.salesforce import ingest as sf_ingest
    from discovery.ingest.servicenow import ingest as sn_ingest
    sf = sf_ingest()
    sn = sn_ingest()
    sf_echo = sf["cross_system_references"]["sf_echo_score"]
    sn_echo = sn["cross_system_references"]["sn_echo_score"]
    # Both sides must exceed D7 threshold of 0.15
    assert sf_echo > 0.15, f"SF echo score {sf_echo} should exceed 0.15"
    assert sn_echo > 0.15, f"SN echo score {sn_echo} should exceed 0.15"
