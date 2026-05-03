"""
SN-CONNECT-1 + JIRA-CONNECT-1 — Live Connector Health Check Tests
Sprint 5 — Track C

Tests:
  1. No credentials → status="fixture" (graceful fallback)
  2. Credentials set + 200 response → status="live" with latency
  3. Credentials set + 401 response → status="error" auth message
  4. Credentials set + 429 response → status="error" rate limit message
  5. Credentials set + connection error → status="error" network message
  6. Credentials set + timeout → status="error" timeout message
  7. check_all_connectors returns both systems
  8. ConnectorHealth.to_dict() shape correct
  9. is_live property correct for each status

Run:
  pytest tests/contract/test_connector_health.py -v
"""
from __future__ import annotations

import pytest
from unittest.mock import patch, MagicMock

from discovery.ingest.connector_health import (
    ConnectorHealth,
    check_servicenow,
    check_jira,
    check_all_connectors,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

def mock_200(json_data=None, latency=0.05):
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = json_data or {}
    return resp

def mock_status(code):
    resp = MagicMock()
    resp.status_code = code
    return resp

def patch_sn_env(url="https://test.service-now.com", token="test-token"):
    return patch.dict("os.environ", {
        "SERVICENOW_URL": url,
        "SERVICENOW_TOKEN": token,
    })

def patch_jira_env(url="https://test.atlassian.net", token="test-token"):
    return patch.dict("os.environ", {
        "JIRA_URL": url,
        "JIRA_TOKEN": token,
    })

def clear_sn_env():
    return patch.dict("os.environ", {}, clear=True)


# ── ConnectorHealth model ─────────────────────────────────────────────────────

class TestConnectorHealthModel:

    def test_is_live_true_when_status_live(self):
        h = ConnectorHealth(system="ServiceNow", status="live", message="ok", latency_ms=42)
        assert h.is_live is True

    def test_is_live_false_when_fixture(self):
        h = ConnectorHealth(system="Jira", status="fixture", message="no creds")
        assert h.is_live is False

    def test_is_live_false_when_error(self):
        h = ConnectorHealth(system="ServiceNow", status="error", message="auth fail")
        assert h.is_live is False

    def test_to_dict_shape(self):
        h = ConnectorHealth(system="Jira", status="live", message="ok", latency_ms=38)
        d = h.to_dict()
        assert d["system"] == "Jira"
        assert d["status"] == "live"
        assert d["isLive"] is True
        assert d["latencyMs"] == 38
        assert "message" in d

    def test_to_dict_latency_none_when_fixture(self):
        h = ConnectorHealth(system="ServiceNow", status="fixture", message="no url")
        d = h.to_dict()
        assert d["latencyMs"] is None
        assert d["isLive"] is False


# ── ServiceNow health checks ──────────────────────────────────────────────────

class TestServiceNowHealth:

    def test_no_url_returns_fixture(self):
        import os
        env = {k: v for k, v in os.environ.items()
               if k not in ("SERVICENOW_URL", "SERVICENOW_TOKEN", "SERVICENOW_USER", "SERVICENOW_PASS")}
        with patch.dict(os.environ, env, clear=True):
            result = check_servicenow()
        assert result.status == "fixture"
        assert result.system == "ServiceNow"
        assert result.is_live is False

    def test_url_but_no_credentials_returns_fixture(self):
        import os
        env = {k: v for k, v in os.environ.items()
               if k not in ("SERVICENOW_TOKEN", "SERVICENOW_USER", "SERVICENOW_PASS")}
        env["SERVICENOW_URL"] = "https://test.service-now.com"
        with patch.dict(os.environ, env, clear=True):
            result = check_servicenow()
        assert result.status == "fixture"

    def test_200_response_returns_live(self):
        with patch_sn_env():
            with patch("requests.get", return_value=mock_200()) as mock_get:
                result = check_servicenow()
        assert result.status == "live"
        assert result.is_live is True
        assert result.latency_ms is not None
        assert result.latency_ms >= 0

    def test_401_returns_error_auth_message(self):
        with patch_sn_env():
            with patch("requests.get", return_value=mock_status(401)):
                result = check_servicenow()
        assert result.status == "error"
        assert "auth" in result.message.lower() or "401" in result.message.lower()

    def test_429_returns_error_rate_limit_message(self):
        with patch_sn_env():
            with patch("requests.get", return_value=mock_status(429)):
                result = check_servicenow()
        assert result.status == "error"
        assert "rate" in result.message.lower() or "429" in result.message.lower()

    def test_connection_error_returns_error(self):
        import requests as req
        with patch_sn_env():
            with patch("requests.get", side_effect=req.exceptions.ConnectionError("refused")):
                result = check_servicenow()
        assert result.status == "error"
        assert "reach" in result.message.lower() or "connect" in result.message.lower()

    def test_timeout_returns_error(self):
        import requests as req
        with patch_sn_env():
            with patch("requests.get", side_effect=req.exceptions.Timeout()):
                result = check_servicenow()
        assert result.status == "error"
        assert "timeout" in result.message.lower() or "timed out" in result.message.lower()

    def test_bearer_token_used_in_header(self):
        with patch_sn_env(token="my-sn-token"):
            with patch("requests.get", return_value=mock_200()) as mock_get:
                check_servicenow()
        call_kwargs = mock_get.call_args
        headers = call_kwargs[1].get("headers") or call_kwargs[0][1] if len(call_kwargs[0]) > 1 else {}
        headers = call_kwargs.kwargs.get("headers", call_kwargs.args[1] if len(call_kwargs.args) > 1 else {})
        assert "Bearer my-sn-token" in str(headers)


# ── Jira health checks ────────────────────────────────────────────────────────

class TestJiraHealth:

    def test_no_url_returns_fixture(self):
        import os
        env = {k: v for k, v in os.environ.items()
               if k not in ("JIRA_URL", "JIRA_TOKEN")}
        with patch.dict(os.environ, env, clear=True):
            result = check_jira()
        assert result.status == "fixture"
        assert result.system == "Jira"
        assert result.is_live is False

    def test_url_but_no_token_returns_fixture(self):
        import os
        env = {k: v for k, v in os.environ.items() if k != "JIRA_TOKEN"}
        env["JIRA_URL"] = "https://test.atlassian.net"
        with patch.dict(os.environ, env, clear=True):
            result = check_jira()
        assert result.status == "fixture"

    def test_200_response_returns_live(self):
        with patch_jira_env():
            with patch("requests.get",
                       return_value=mock_200({"displayName": "Test User"})):
                result = check_jira()
        assert result.status == "live"
        assert result.is_live is True
        assert result.latency_ms is not None
        assert "Test User" in result.message

    def test_401_returns_error(self):
        with patch_jira_env():
            with patch("requests.get", return_value=mock_status(401)):
                result = check_jira()
        assert result.status == "error"
        assert "auth" in result.message.lower() or "401" in result.message.lower()

    def test_429_returns_error(self):
        with patch_jira_env():
            with patch("requests.get", return_value=mock_status(429)):
                result = check_jira()
        assert result.status == "error"
        assert "rate" in result.message.lower() or "429" in result.message.lower()

    def test_connection_error_returns_error(self):
        import requests as req
        with patch_jira_env():
            with patch("requests.get", side_effect=req.exceptions.ConnectionError()):
                result = check_jira()
        assert result.status == "error"

    def test_timeout_returns_error(self):
        import requests as req
        with patch_jira_env():
            with patch("requests.get", side_effect=req.exceptions.Timeout()):
                result = check_jira()
        assert result.status == "error"
        assert "timeout" in result.message.lower() or "timed out" in result.message.lower()


# ── check_all_connectors ──────────────────────────────────────────────────────

class TestCheckAllConnectors:

    def test_returns_both_systems(self):
        import os
        env = {k: v for k, v in os.environ.items()
               if k not in ("SERVICENOW_URL", "SERVICENOW_TOKEN",
                            "JIRA_URL", "JIRA_TOKEN")}
        with patch.dict(os.environ, env, clear=True):
            result = check_all_connectors()
        assert "ServiceNow" in result
        assert "Jira" in result

    def test_both_fixture_when_no_creds(self):
        import os
        env = {k: v for k, v in os.environ.items()
               if k not in ("SERVICENOW_URL", "SERVICENOW_TOKEN",
                            "JIRA_URL", "JIRA_TOKEN")}
        with patch.dict(os.environ, env, clear=True):
            result = check_all_connectors()
        assert result["ServiceNow"]["status"] == "fixture"
        assert result["Jira"]["status"] == "fixture"

    def test_both_live_when_creds_and_200(self):
        with patch_sn_env(), patch_jira_env():
            with patch("requests.get",
                       return_value=mock_200({"displayName": "User"})):
                result = check_all_connectors()
        assert result["ServiceNow"]["status"] == "live"
        assert result["Jira"]["status"] == "live"
        assert result["ServiceNow"]["isLive"] is True
        assert result["Jira"]["isLive"] is True

    def test_result_has_required_keys(self):
        import os
        env = {k: v for k, v in os.environ.items()
               if k not in ("SERVICENOW_URL", "JIRA_URL")}
        with patch.dict(os.environ, env, clear=True):
            result = check_all_connectors()
        for system in ("ServiceNow", "Jira"):
            assert "status" in result[system]
            assert "message" in result[system]
            assert "isLive" in result[system]
            assert "latencyMs" in result[system]
