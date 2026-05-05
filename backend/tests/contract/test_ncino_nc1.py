"""
ENG-AIQ-NC-1 - nCino Salesforce ingestor tests.

Run:
  pytest tests/contract/test_ncino_nc1.py -v
"""
from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest


def mock_200_ncino():
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"totalSize": 1, "records": [{"Id": "a00TEST"}]}
    return resp


def mock_status(code):
    resp = MagicMock()
    resp.status_code = code
    return resp


def patch_sf_env(url="https://test.my.salesforce.com", token="test-sf-token"):
    return patch.dict(
        "os.environ",
        {
            "SF_INSTANCE_URL": url,
            "SF_ACCESS_TOKEN": token,
        },
    )


class TestNcinoHealthCheck:
    def _check(self):
        from discovery.ingest.connector_health import check_ncino

        return check_ncino

    def test_no_url_returns_fixture(self):
        env = {
            k: v
            for k, v in os.environ.items()
            if k not in ("SF_INSTANCE_URL", "SF_ACCESS_TOKEN")
        }
        with patch.dict(os.environ, env, clear=True):
            result = self._check()()

        assert result.status == "fixture"
        assert result.system == "nCino"
        assert result.is_live is False

    def test_url_but_no_token_returns_fixture(self):
        env = {k: v for k, v in os.environ.items() if k != "SF_ACCESS_TOKEN"}
        env["SF_INSTANCE_URL"] = "https://test.my.salesforce.com"
        with patch.dict(os.environ, env, clear=True):
            result = self._check()()

        assert result.status == "fixture"

    def test_200_returns_live(self):
        with patch_sf_env():
            with patch("requests.get", return_value=mock_200_ncino()):
                result = self._check()()

        assert result.status == "live"
        assert result.is_live is True
        assert result.latency_ms is not None
        assert "LLC_BI__Loan__c" in result.message

    def test_401_returns_error_auth(self):
        with patch_sf_env():
            with patch("requests.get", return_value=mock_status(401)):
                result = self._check()()

        assert result.status == "error"
        assert "auth" in result.message.lower() or "token" in result.message.lower()

    def test_404_returns_error_ncino_not_installed(self):
        with patch_sf_env():
            with patch("requests.get", return_value=mock_status(404)):
                result = self._check()()

        assert result.status == "error"
        assert "ncino" in result.message.lower() or "not found" in result.message.lower()

    def test_429_returns_error_rate_limit(self):
        with patch_sf_env():
            with patch("requests.get", return_value=mock_status(429)):
                result = self._check()()

        assert result.status == "error"
        assert "rate" in result.message.lower() or "429" in result.message.lower()

    def test_connection_error_returns_error(self):
        import requests as req

        with patch_sf_env():
            with patch("requests.get", side_effect=req.exceptions.ConnectionError()):
                result = self._check()()

        assert result.status == "error"

    def test_timeout_returns_error(self):
        import requests as req

        with patch_sf_env():
            with patch("requests.get", side_effect=req.exceptions.Timeout()):
                result = self._check()()

        assert result.status == "error"
        assert "timeout" in result.message.lower() or "timed out" in result.message.lower()

    def test_soql_permission_failure_returns_error(self):
        describe_ok = MagicMock()
        describe_ok.status_code = 200
        soql_fail = MagicMock()
        soql_fail.status_code = 400

        with patch_sf_env():
            with patch("requests.get", side_effect=[describe_ok, soql_fail]):
                result = self._check()()

        assert result.status == "error"
        assert "permission" in result.message.lower() or "soql" in result.message.lower()

    def test_live_message_confirms_queryable(self):
        with patch_sf_env():
            with patch("requests.get", return_value=mock_200_ncino()):
                result = self._check()()

        assert result.status == "live"
        assert "queryable" in result.message.lower() or "accessible" in result.message.lower()


class TestCheckAllConnectorsNcino:
    def test_check_all_includes_ncino(self):
        env = {
            k: v
            for k, v in os.environ.items()
            if k
            not in (
                "SF_INSTANCE_URL",
                "SF_ACCESS_TOKEN",
                "SERVICENOW_URL",
                "JIRA_URL",
            )
        }
        with patch.dict(os.environ, env, clear=True):
            from discovery.ingest.connector_health import check_all_connectors

            result = check_all_connectors()

        assert "nCino" in result
        assert "ServiceNow" in result
        assert "Jira" in result

    def test_ncino_fixture_when_no_creds(self):
        env = {
            k: v
            for k, v in os.environ.items()
            if k not in ("SF_INSTANCE_URL", "SF_ACCESS_TOKEN")
        }
        with patch.dict(os.environ, env, clear=True):
            from discovery.ingest.connector_health import check_all_connectors

            result = check_all_connectors()

        assert result["nCino"]["status"] == "fixture"
        assert result["nCino"]["isLive"] is False

    def test_ncino_live_when_creds_and_200(self):
        with patch_sf_env():
            with patch("requests.get", return_value=mock_200_ncino()):
                from discovery.ingest.connector_health import check_all_connectors

                result = check_all_connectors()

        assert result["nCino"]["status"] == "live"
        assert result["nCino"]["isLive"] is True


class TestNcinoIngestorOffline:
    def _ingest(self):
        os.environ["INGEST_MODE"] = "offline"
        try:
            from discovery.ingest.ncino import ingest

            return ingest()
        finally:
            os.environ.pop("INGEST_MODE", None)

    def test_offline_ingest_returns_dict(self):
        result = self._ingest()

        assert isinstance(result, dict)

    def test_offline_ingest_has_all_metric_keys(self):
        result = self._ingest()
        required_keys = [
            "loans",
            "loan_stage_history",
            "covenant_compliance",
            "checklists",
            "spread_periods",
            "process_instances",
            "origination_metrics",
            "covenant_metrics",
            "checklist_metrics",
            "spreading_metrics",
            "approval_metrics",
        ]

        for key in required_keys:
            assert key in result, f"Missing key: {key}"

    def test_origination_metrics_has_required_fields(self):
        result = self._ingest()
        metrics = result["origination_metrics"]

        for field in ["total_loans", "avg_stage_transitions", "max_stage_transitions"]:
            assert field in metrics, f"origination_metrics missing: {field}"

    def test_covenant_metrics_has_required_fields(self):
        result = self._ingest()
        metrics = result["covenant_metrics"]

        for field in ["total_covenants", "overdue_count", "breached_count"]:
            assert field in metrics, f"covenant_metrics missing: {field}"

    def test_checklist_metrics_has_required_fields(self):
        result = self._ingest()
        metrics = result["checklist_metrics"]

        for field in ["total_checklists", "overrun_count", "stalled_count"]:
            assert field in metrics, f"checklist_metrics missing: {field}"

    def test_spreading_metrics_has_required_fields(self):
        result = self._ingest()
        metrics = result["spreading_metrics"]

        for field in ["total_periods", "unlocked_count", "max_days_unlocked"]:
            assert field in metrics, f"spreading_metrics missing: {field}"

    def test_approval_metrics_has_required_fields(self):
        result = self._ingest()
        metrics = result["approval_metrics"]

        for field in ["total_instances", "pending_count"]:
            assert field in metrics, f"approval_metrics missing: {field}"

    def test_offline_loans_present(self):
        result = self._ingest()

        assert len(result.get("loans", [])) > 0

    def test_offline_detectors_fire_from_ingest(self):
        result = self._ingest()
        sf_data = {"ncino": result}

        from discovery.detectors.approval_bottleneck import detect as det_apr
        from discovery.detectors.checklist_bottleneck import detect as det_chk
        from discovery.detectors.covenant_tracking_gap import detect as det_cov
        from discovery.detectors.loan_origination_routing_friction import (
            detect as det_rtg,
        )
        from discovery.detectors.spreading_bottleneck import detect as det_spr

        fired = []
        for name, detector in [
            ("covenant", det_cov),
            ("checklist", det_chk),
            ("spreading", det_spr),
            ("approval", det_apr),
            ("routing", det_rtg),
        ]:
            if detector(sf_data):
                fired.append(name)

        assert len(fired) > 0, f"At least 1 detector should fire. Fired: {fired}"


class TestNcinoLiveModeCredentials:
    def test_live_mode_requires_instance_url(self):
        env = {
            k: v
            for k, v in os.environ.items()
            if k not in ("SF_INSTANCE_URL", "SF_ACCESS_TOKEN")
        }
        env["INGEST_MODE"] = "live"

        with patch.dict(os.environ, env, clear=True):
            from discovery.ingest.ncino import NcinoIngestError, _get_client

            with pytest.raises(NcinoIngestError):
                _get_client()

    def test_live_mode_requires_access_token(self):
        env = {k: v for k, v in os.environ.items() if k != "SF_ACCESS_TOKEN"}
        env["INGEST_MODE"] = "live"
        env["SF_INSTANCE_URL"] = "https://test.my.salesforce.com"

        with patch.dict(os.environ, env, clear=True):
            from discovery.ingest.ncino import NcinoIngestError, _get_client

            with pytest.raises(NcinoIngestError):
                _get_client()
