"""
SF-3.1 tests — Live Org Validator + Ingestion Shape Contracts.
All tests run offline. No credentials required.
"""
from __future__ import annotations
import json
import os
import pytest

os.environ["INGEST_MODE"] = "offline"


# ─────────────────────────────────────────────────────────────────────────────
# Shape contract tests — all 7 ingestion functions
# ─────────────────────────────────────────────────────────────────────────────

class TestIngestionShapes:

    def test_get_case_metrics_shape(self):
        from discovery.ingest.salesforce import get_case_metrics
        data = get_case_metrics()
        for k in ["total_cases_90d", "closed_cases_90d", "owner_changes_90d",
                  "handoff_score", "cases_with_kb_link", "knowledge_gap_score",
                  "category_breakdown"]:
            assert k in data

    def test_get_case_metrics_types(self):
        from discovery.ingest.salesforce import get_case_metrics
        data = get_case_metrics()
        assert isinstance(data["total_cases_90d"],    int)
        assert isinstance(data["handoff_score"],       float)
        assert isinstance(data["knowledge_gap_score"], float)
        assert isinstance(data["category_breakdown"],  list)

    def test_get_flow_inventory_shape(self):
        from discovery.ingest.salesforce import get_flow_inventory
        data = get_flow_inventory()
        for k in ["active_flow_count_on_object", "avg_element_count",
                  "flow_activity_score", "trigger_object", "flows"]:
            assert k in data
        assert isinstance(data["flows"], list)

    def test_get_approval_pending_is_list(self):
        from discovery.ingest.salesforce import get_approval_pending
        data = get_approval_pending()
        assert isinstance(data, list)
        if data:
            for k in ["process_name", "pending_count", "avg_delay_days",
                      "approver_count", "bottleneck_score"]:
                assert k in data[0]

    def test_get_approval_pending_empty_is_valid(self):
        """Empty list from approval_pending is a valid response — do not fail."""
        from discovery.ingest.live_validator import _validate_shape
        passed, issues = _validate_shape("get_approval_pending", [])
        assert passed
        assert issues == []

    def test_get_knowledge_coverage_shape(self):
        from discovery.ingest.salesforce import get_knowledge_coverage
        data = get_knowledge_coverage()
        for k in ["closed_cases_90d", "cases_with_kb_link", "knowledge_gap_score"]:
            assert k in data

    def test_get_named_credentials_is_list(self):
        from discovery.ingest.salesforce import get_named_credentials
        data = get_named_credentials()
        assert isinstance(data, list)
        if data:
            for k in ["credential_name", "credential_developer_name"]:
                assert k in data[0]

    def test_get_named_credential_flow_refs_shape(self):
        from discovery.ingest.salesforce import (
            get_named_credentials, get_named_credential_flow_refs
        )
        creds = get_named_credentials()
        data = get_named_credential_flow_refs(creds)
        assert isinstance(data, list)
        if data:
            for k in ["credential_name", "flow_reference_count", "referencing_flow_ids"]:
                assert k in data[0]

    def test_get_cross_system_references_shape(self):
        from discovery.ingest.salesforce import get_cross_system_references
        data = get_cross_system_references()
        for k in ["sf_echo_count", "sf_total_cases", "sf_echo_score",
                  "matched_patterns", "sample_matches"]:
            assert k in data

    def test_cross_system_score_valid_ratio(self):
        from discovery.ingest.salesforce import get_cross_system_references
        data = get_cross_system_references()
        assert 0.0 <= data["sf_echo_score"] <= 1.0

    def test_ingest_top_level_keys(self):
        from discovery.ingest.salesforce import ingest
        data = ingest()
        for k in ["case_metrics", "flow_inventory", "approval_processes",
                  "named_credentials", "cross_system_references"]:
            assert k in data


# ─────────────────────────────────────────────────────────────────────────────
# Dimension A / B split — core logic
# ─────────────────────────────────────────────────────────────────────────────

class TestDimensionSplit:
    """EMPTY status is a Dimension B warning — NOT a Dimension A failure."""

    def test_empty_result_does_not_block_sprint(self):
        from discovery.ingest.live_validator import FunctionResult
        r = FunctionResult("get_approval_pending")
        r.status = "EMPTY"
        assert not r.is_hard_failure   # EMPTY never blocks
        assert r.is_empty_warning      # but it does warn

    def test_error_is_hard_failure(self):
        from discovery.ingest.live_validator import FunctionResult
        r = FunctionResult("get_flow_inventory")
        r.status = "ERROR"
        assert r.is_hard_failure
        assert not r.is_empty_warning

    def test_shape_error_is_hard_failure(self):
        from discovery.ingest.live_validator import FunctionResult
        r = FunctionResult("get_case_metrics")
        r.status = "SHAPE_ERROR"
        assert r.is_hard_failure

    def test_ok_is_not_hard_failure(self):
        from discovery.ingest.live_validator import FunctionResult
        r = FunctionResult("get_case_metrics")
        r.status = "OK"
        assert not r.is_hard_failure
        assert not r.is_empty_warning


# ─────────────────────────────────────────────────────────────────────────────
# Shape validator
# ─────────────────────────────────────────────────────────────────────────────

class TestShapeValidator:

    def test_case_metrics_valid_shape(self):
        from discovery.ingest.live_validator import _validate_shape
        from discovery.ingest.salesforce import get_case_metrics
        passed, issues = _validate_shape("get_case_metrics", get_case_metrics())
        assert passed, f"Shape issues: {issues}"

    def test_flow_inventory_valid_shape(self):
        from discovery.ingest.live_validator import _validate_shape
        from discovery.ingest.salesforce import get_flow_inventory
        passed, issues = _validate_shape("get_flow_inventory", get_flow_inventory())
        assert passed, f"Shape issues: {issues}"

    def test_cross_system_valid_shape(self):
        from discovery.ingest.live_validator import _validate_shape
        from discovery.ingest.salesforce import get_cross_system_references
        passed, issues = _validate_shape("get_cross_system_references", get_cross_system_references())
        assert passed, f"Shape issues: {issues}"

    def test_invalid_shape_detected(self):
        from discovery.ingest.live_validator import _validate_shape
        passed, issues = _validate_shape("get_case_metrics", {"wrong_key": 1})
        assert not passed
        assert len(issues) > 0

    def test_empty_list_passes_shape_check(self):
        from discovery.ingest.live_validator import _validate_shape
        # Empty list is correct shape for list-type functions
        passed, issues = _validate_shape("get_approval_pending", [])
        assert passed

    def test_empty_list_for_named_credentials_is_valid(self):
        from discovery.ingest.live_validator import _validate_shape
        passed, issues = _validate_shape("get_named_credentials", [])
        assert passed


# ─────────────────────────────────────────────────────────────────────────────
# FunctionResult log format
# ─────────────────────────────────────────────────────────────────────────────

class TestFunctionResult:

    def test_ok_log_line_has_checkmark(self):
        from discovery.ingest.live_validator import FunctionResult
        r = FunctionResult("get_case_metrics")
        r.status, r.row_count, r.elapsed_ms = "OK", 300, 412
        line = r.log_line()
        assert "\u2705" in line
        assert "rows=300" in line
        assert "ms=412" in line
        assert "status=OK" in line

    def test_empty_log_line_has_warning_symbol(self):
        from discovery.ingest.live_validator import FunctionResult
        r = FunctionResult("get_approval_pending")
        r.status, r.row_count, r.elapsed_ms = "EMPTY", 0, 88
        line = r.log_line()
        assert "\u26a0" in line
        assert "EMPTY" in line

    def test_error_log_line_has_cross(self):
        from discovery.ingest.live_validator import FunctionResult
        r = FunctionResult("get_flow_inventory")
        r.status = "ERROR"
        r.error = "SOQL query failed: connection timeout"
        line = r.log_line()
        assert "ERROR" in line
        assert "get_flow_inventory" in line

    def test_retry_count_in_log_line(self):
        from discovery.ingest.live_validator import FunctionResult
        r = FunctionResult("get_case_metrics")
        r.status, r.retries = "OK", 2
        line = r.log_line()
        assert "retries=2" in line

    def test_to_dict_completeness(self):
        from discovery.ingest.live_validator import FunctionResult
        r = FunctionResult("get_cross_system_references")
        r.status = "OK"
        d = r.to_dict()
        for k in ["function", "status", "row_count", "elapsed_ms",
                  "retries", "error", "shape_issues", "warnings"]:
            assert k in d


# ─────────────────────────────────────────────────────────────────────────────
# Gate and seed instruction logic
# ─────────────────────────────────────────────────────────────────────────────

class TestValidatorGates:

    def test_missing_credentials_fails_dim_a(self, monkeypatch):
        monkeypatch.delenv("SF_INSTANCE_URL", raising=False)
        monkeypatch.delenv("SF_ACCESS_TOKEN",  raising=False)
        monkeypatch.setenv("INGEST_MODE", "live")
        from discovery.ingest.live_validator import run_validation
        report = run_validation()
        assert not report["sf31_passed"]
        assert not report["dim_a_passed"]
        assert not report["gates"]["credentials_present"]

    def test_report_has_dim_a_and_dim_b_fields(self, monkeypatch):
        monkeypatch.delenv("SF_INSTANCE_URL", raising=False)
        monkeypatch.delenv("SF_ACCESS_TOKEN",  raising=False)
        from discovery.ingest.live_validator import run_validation
        report = run_validation()
        assert "dim_a_passed" in report
        assert "dim_b_passed" in report
        assert "sf31_passed" in report

    def test_seed_instructions_low_volume(self):
        from discovery.ingest.live_validator import _build_seed_instructions
        instructions = _build_seed_instructions(
            5, 0,
            {"minimum_volume_cases": False, "volume_cases_sufficient": False,
             "minimum_volume_flows": False, "volume_flows_sufficient": False}
        )
        assert len(instructions) >= 2
        assert any("Case" in i for i in instructions)
        assert any("Flow" in i for i in instructions)

    def test_seed_instructions_sufficient_volume(self):
        from discovery.ingest.live_validator import _build_seed_instructions
        instructions = _build_seed_instructions(
            300, 4,
            {"volume_cases_sufficient": True, "volume_flows_sufficient": True}
        )
        assert "No seeding required" in instructions[0]

    def test_summary_contains_status(self, monkeypatch):
        monkeypatch.delenv("SF_INSTANCE_URL", raising=False)
        monkeypatch.delenv("SF_ACCESS_TOKEN",  raising=False)
        from discovery.ingest.live_validator import run_validation
        report = run_validation()
        assert "SF-3.1" in report["summary"]
        assert "FAILED" in report["summary"] or "PASSED" in report["summary"]

    def test_summary_warns_on_low_volume(self):
        from discovery.ingest.live_validator import _build_summary
        summary = _build_summary(
            {"volume_cases_sufficient": False}, [], False, False
        )
        assert "\u26a0" in summary or "Volume" in summary


# ─────────────────────────────────────────────────────────────────────────────
# Retry wrapper
# ─────────────────────────────────────────────────────────────────────────────

class TestRetryWrapper:

    def test_retries_on_429(self, monkeypatch):
        """Function that fails with 429 on first call succeeds on second."""
        from discovery.ingest.salesforce import IngestError
        from discovery.ingest.live_validator import _with_retry, FunctionResult

        call_count = [0]
        def flaky():
            call_count[0] += 1
            if call_count[0] < 2:
                raise IngestError("429 rate limited")
            return {"result": "ok"}

        monkeypatch.setattr(
            "discovery.ingest.live_validator.RETRY_BACKOFF", [0.0, 0.0]
        )
        result = FunctionResult("test_fn")
        data = _with_retry("test_fn", flaky, result)
        assert data == {"result": "ok"}
        assert result.retries == 1

    def test_non_transient_error_not_retried(self, monkeypatch):
        """SOQL field error should NOT be retried."""
        from discovery.ingest.salesforce import IngestError
        from discovery.ingest.live_validator import _with_retry, FunctionResult

        call_count = [0]
        def broken():
            call_count[0] += 1
            raise IngestError("INVALID_FIELD: field does not exist")

        monkeypatch.setattr(
            "discovery.ingest.live_validator.RETRY_BACKOFF", [0.0, 0.0]
        )
        result = FunctionResult("test_fn")
        with pytest.raises(IngestError):
            _with_retry("test_fn", broken, result)
        assert call_count[0] == 1  # no retry on non-transient
        assert result.retries == 0

    def test_exhaust_retries_then_raise(self, monkeypatch):
        """After MAX_RETRIES, the last exception is raised."""
        from discovery.ingest.salesforce import IngestError
        from discovery.ingest.live_validator import (
            _with_retry, FunctionResult, MAX_RETRIES
        )

        call_count = [0]
        def always_503():
            call_count[0] += 1
            raise IngestError("503 service unavailable")

        monkeypatch.setattr(
            "discovery.ingest.live_validator.RETRY_BACKOFF", [0.0, 0.0]
        )
        result = FunctionResult("test_fn")
        with pytest.raises(IngestError):
            _with_retry("test_fn", always_503, result)
        assert call_count[0] == MAX_RETRIES + 1


# ─────────────────────────────────────────────────────────────────────────────
# Regression: offline pipeline unaffected
# ─────────────────────────────────────────────────────────────────────────────

class TestOfflineRegression:

    def test_offline_ingest_still_works(self):
        os.environ["INGEST_MODE"] = "offline"
        from discovery.ingest.salesforce import ingest
        data = ingest()
        assert data["case_metrics"]["total_cases_90d"] > 0

    def test_full_pipeline_7_detectors_unaffected(self):
        os.environ["INGEST_MODE"] = "offline"
        from discovery.runner import run
        payload = run(mode="offline", run_id="sf31-regression")
        assert len(payload["opportunities"]) >= 7
