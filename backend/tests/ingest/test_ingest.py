import pytest
import os
from unittest.mock import patch, MagicMock

from discovery.ingest.servicenow import get_incident_metrics, get_cross_system_references
from discovery.ingest.jira import get_issue_metrics, get_sprint_velocity
from discovery.types import IngestError

@pytest.fixture
def offline_env():
    """Forces the modules into offline mode."""
    with patch.dict(os.environ, {"INGEST_MODE": "offline"}):
        yield

@pytest.fixture
def live_missing_creds_env():
    """Forces live mode but removes all credentials."""
    with patch.dict(os.environ, {"INGEST_MODE": "live"}):
        for key in ["SERVICENOW_URL", "SERVICENOW_TOKEN", "JIRA_URL", "JIRA_TOKEN", "JIRA_BOARD_ID"]:
            os.environ.pop(key, None)
        yield

@pytest.fixture
def live_with_creds_env():
    """Forces live mode with fake credentials."""
    fake_creds = {
        "INGEST_MODE": "live",
        "SERVICENOW_URL": "https://fake.service-now.com",
        "SERVICENOW_TOKEN": "fake-token",
        "JIRA_URL": "https://fake.jira.com",
        "JIRA_TOKEN": "fake-token",
        "JIRA_BOARD_ID": "123"
    }
    with patch.dict(os.environ, fake_creds):
        yield

# --- OFFLINE TESTS ---
def test_offline_servicenow_returns_fixtures(offline_env):
    incidents = get_incident_metrics()
    assert len(incidents) > 0
    assert incidents[0]["category"] == "Access"

def test_offline_jira_returns_fixtures(offline_env):
    metrics = get_issue_metrics()
    assert len(metrics) > 0
    assert metrics[0]["project"] == "CRM"

# --- LIVE MODE MISSING CREDS TESTS ---
@patch("discovery.ingest.servicenow.warn")
def test_live_servicenow_missing_creds_skips_gracefully(mock_warn, live_missing_creds_env):
    result = get_incident_metrics()
    assert result == []
    mock_warn.assert_called_once()

@patch("discovery.ingest.jira.warn")
def test_live_jira_missing_creds_skips_gracefully(mock_warn, live_missing_creds_env):
    result = get_issue_metrics()
    assert result == []
    mock_warn.assert_called_once()

# --- LIVE MODE API FAILS TESTS ---
@patch("discovery.ingest.servicenow.requests.get")
def test_live_servicenow_api_failure_raises_error(mock_get, live_with_creds_env):
    mock_response = MagicMock()
    mock_response.raise_for_status.side_effect = Exception("API Failed")
    mock_get.return_value = mock_response

    with pytest.raises(IngestError) as exc_info:
        get_incident_metrics()

    assert "ServiceNow get_incident_metrics failed" in str(exc_info.value)

@patch("discovery.ingest.jira.requests.get")
def test_live_jira_api_failure_raises_error(mock_get, live_with_creds_env):
    mock_response = MagicMock()
    mock_response.raise_for_status.side_effect = Exception("API Failed")
    mock_get.return_value = mock_response

    with pytest.raises(IngestError) as exc_info:
        get_issue_metrics()

    assert "Jira get_issue_metrics failed" in str(exc_info.value)
