"""
SF-3.1 — Live Org Validator

Validates that a real Salesforce org is ready for AgentIQ discovery.

Validation is split into two independent dimensions:

  DIMENSION A — Connectivity & Query Correctness (must pass / hard gates)
    - Credentials present
    - API connectivity confirmed
    - All 7 ingestion functions execute without error
    - All 7 functions return the correct documented shape

  DIMENSION B — Data Volume (best-effort / warnings only)
    - Case volume ≥ 20 (needed for meaningful D2/D4/D7 signal)
    - Active flows ≥ 1 (needed for D1 signal)
    - If empty: emits ⚠ warning + seeding instructions; does NOT fail the sprint

An org that passes Dimension A and fails Dimension B is still a PASS for SF-3.1.
It means: auth works, queries work, shapes are correct. Seed data is needed
before calibration (SF-3.2) — not before connectivity is confirmed (SF-3.1).

Usage:
    python -m backend.discovery.ingest.live_validator
    python -m backend.discovery.ingest.live_validator --check-only
    python -m backend.discovery.ingest.live_validator --report-path runs/sf31_report.json

See AUTH_SETUP.md for how to obtain SF_INSTANCE_URL and SF_ACCESS_TOKEN.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Volume thresholds — informational only, never block the sprint
# ─────────────────────────────────────────────────────────────────────────────

MIN_CASES_90D    = 20
MIN_ACTIVE_FLOWS = 1

# Retry settings for transient 429/503 errors
MAX_RETRIES    = 2
RETRY_BACKOFF  = [1.0, 3.0]   # seconds between retries

# ─────────────────────────────────────────────────────────────────────────────
# Shape contracts
# ─────────────────────────────────────────────────────────────────────────────

SHAPE_CONTRACTS: Dict[str, Dict] = {
    "get_case_metrics": {
        "required_keys": [
            "total_cases_90d", "closed_cases_90d", "owner_changes_90d",
            "handoff_score", "cases_with_kb_link", "knowledge_gap_score",
            "category_breakdown",
        ],
        "type_checks": {
            "total_cases_90d":    int,
            "closed_cases_90d":   int,
            "owner_changes_90d":  int,
            "handoff_score":      float,
            "knowledge_gap_score":float,
            "category_breakdown": list,
        },
    },
    "get_flow_inventory": {
        "required_keys": [
            "active_flow_count_on_object", "avg_element_count",
            "flow_activity_score", "trigger_object", "flows",
        ],
        "type_checks": {
            "active_flow_count_on_object": int,
            "flow_activity_score":         float,
            "flows":                       list,
        },
    },
    "get_approval_pending": {
        "is_list": True,
        "list_item_keys": [
            "process_name", "pending_count", "avg_delay_days",
            "approver_count", "bottleneck_score",
        ],
    },
    "get_knowledge_coverage": {
        "required_keys": [
            "closed_cases_90d", "cases_with_kb_link", "knowledge_gap_score",
        ],
        "type_checks": {
            "closed_cases_90d":   int,
            "cases_with_kb_link": int,
            "knowledge_gap_score":float,
        },
    },
    "get_named_credentials": {
        "is_list": True,
        "list_item_keys": ["credential_name", "credential_developer_name"],
    },
    "get_named_credential_flow_refs": {
        "is_list": True,
        "list_item_keys": [
            "credential_name", "flow_reference_count", "referencing_flow_ids",
        ],
    },
    "get_cross_system_references": {
        "required_keys": [
            "sf_echo_count", "sf_total_cases", "sf_echo_score",
            "matched_patterns", "sample_matches",
        ],
        "type_checks": {
            "sf_echo_count":  int,
            "sf_total_cases": int,
            "sf_echo_score":  float,
        },
    },
}


# ─────────────────────────────────────────────────────────────────────────────
# FunctionResult — captures everything about one ingestion call
# ─────────────────────────────────────────────────────────────────────────────

class FunctionResult:
    # Status meanings:
    #   OK           — executed, returned data, shape correct
    #   EMPTY        — executed, returned empty data, shape correct (⚠ warning — not failure)
    #   SHAPE_ERROR  — executed but shape contract violated (hard failure)
    #   ERROR        — raised IngestError (hard failure)
    #   SKIPPED      — not attempted (e.g. check-only mode)

    def __init__(self, name: str):
        self.name          = name
        self.status        = "PENDING"
        self.data          = None
        self.row_count     = 0
        self.elapsed_ms    = 0
        self.retries       = 0
        self.error         = ""
        self.shape_issues: List[str] = []
        self.warnings:     List[str] = []

    # ── dimension classification ──────────────────────────────────────────────

    @property
    def is_hard_failure(self) -> bool:
        """Dimension A failure — blocks the sprint gate."""
        return self.status in ("ERROR", "SHAPE_ERROR")

    @property
    def is_empty_warning(self) -> bool:
        """Dimension B — volume warning only, never blocks."""
        return self.status == "EMPTY"

    # ── log line format from Sprint 3 doc ────────────────────────────────────

    def log_line(self) -> str:
        tag = f"[{self.name}]"
        if self.status == "ERROR":
            retry_note = f" (after {self.retries} retries)" if self.retries else ""
            return f"ERROR {tag:42s} {self.error[:100]}{retry_note}"
        retry_note = f" retries={self.retries}" if self.retries else ""
        icon = {"OK": "\u2705", "EMPTY": "\u26a0", "SHAPE_ERROR": "\u274c"}.get(self.status, "?")
        return (
            f"INFO  {icon} {tag:40s} "
            f"rows={self.row_count:<6} "
            f"ms={self.elapsed_ms:<6} "
            f"status={self.status}{retry_note}"
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "function":     self.name,
            "status":       self.status,
            "row_count":    self.row_count,
            "elapsed_ms":   self.elapsed_ms,
            "retries":      self.retries,
            "error":        self.error,
            "shape_issues": self.shape_issues,
            "warnings":     self.warnings,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Shape validator
# ─────────────────────────────────────────────────────────────────────────────

def _validate_shape(fn_name: str, data: Any) -> Tuple[bool, List[str]]:
    contract = SHAPE_CONTRACTS.get(fn_name)
    if not contract:
        return True, []
    issues = []

    if contract.get("is_list"):
        if not isinstance(data, list):
            return False, [f"Expected list, got {type(data).__name__}"]
        if data:
            item = data[0]
            for key in contract.get("list_item_keys", []):
                if key not in item:
                    issues.append(f"List item missing key: '{key}'")
        return len(issues) == 0, issues

    if not isinstance(data, dict):
        return False, [f"Expected dict, got {type(data).__name__}"]

    for key in contract.get("required_keys", []):
        if key not in data:
            issues.append(f"Missing required key: '{key}'")

    for key, expected_type in contract.get("type_checks", {}).items():
        if key in data and data[key] is not None:
            if not isinstance(data[key], (expected_type, int, float)):
                issues.append(
                    f"Key '{key}': expected {expected_type.__name__}, "
                    f"got {type(data[key]).__name__}"
                )

    return len(issues) == 0, issues


# ─────────────────────────────────────────────────────────────────────────────
# Retry wrapper — handles transient 429 / 503 from Salesforce
# ─────────────────────────────────────────────────────────────────────────────

def _with_retry(fn_name: str, fn_call, result: FunctionResult):
    """
    Execute fn_call() with up to MAX_RETRIES retries on transient errors.
    429 (rate limited) and 503 (service unavailable) are retried.
    Other IngestErrors are raised immediately.
    Updates result.retries in place.
    """
    from .salesforce import IngestError

    last_exc = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            return fn_call()
        except IngestError as e:
            msg = str(e)
            is_transient = any(code in msg for code in ["429", "503", "TIMEOUT", "timeout"])
            if is_transient and attempt < MAX_RETRIES:
                wait = RETRY_BACKOFF[attempt]
                logger.warning(
                    f"[{fn_name}] Transient error (attempt {attempt+1}/{MAX_RETRIES+1}), "
                    f"retrying in {wait}s: {msg[:60]}"
                )
                result.retries += 1
                time.sleep(wait)
                last_exc = e
            else:
                raise
    raise last_exc  # should never reach here


# ─────────────────────────────────────────────────────────────────────────────
# Main validation runner
# ─────────────────────────────────────────────────────────────────────────────

def run_validation(check_only: bool = False) -> Dict[str, Any]:
    """
    Run all SF-3.1 validation checks.

    Returns dict: sf31_passed, dim_a_passed, dim_b_passed (informational),
                  gates, function_results, seed_instructions, report_time, summary.
    """
    os.environ["INGEST_MODE"] = "live"
    from .salesforce import (
        _get_client, IngestError,
        get_case_metrics, get_flow_inventory, get_approval_pending,
        get_knowledge_coverage, get_named_credentials,
        get_named_credential_flow_refs, get_cross_system_references,
    )

    report_time = datetime.now(timezone.utc).isoformat()
    function_results: List[FunctionResult] = []
    gates: Dict[str, bool] = {}

    # ── Dimension A Gate 1: credentials present ───────────────────────────────
    instance_url = os.getenv("SF_INSTANCE_URL", "")
    gates["credentials_present"] = True if instance_url else False

    if not gates["credentials_present"]:
        logger.error(
            "SF_INSTANCE_URL and SF_ACCESS_TOKEN are not set.\n"
            "See AUTH_SETUP.md for three ways to obtain a Bearer token."
        )
        return _build_report(gates, function_results, report_time, False)

    # ── Dimension A Gate 2: API connectivity ──────────────────────────────────
    try:
        client = _get_client()
        import requests
        session = requests.Session()

        for attempt in range(MAX_RETRIES + 1):
            try:
                resp = session.get(
                    instance_url,
                    timeout=15,
                )
                if resp.status_code == 401:
                    raise IngestError(
                        "HTTP 401 Unauthorized. Token may have expired. "
                        "Re-authenticate using AUTH_SETUP.md and re-run."
                    )
                resp.raise_for_status()
                gates["api_connected"] = True
                logger.info(f"\u2705 API connectivity OK ({instance_url})")
                break
            except requests.exceptions.RequestException as e:
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_BACKOFF[attempt])
                else:
                    raise IngestError(f"API connectivity failed after retries: {e}")

    except Exception as e:
        gates["api_connected"] = False
        logger.error(f"\u274c API connectivity FAILED: {e}")
        return _build_report(gates, function_results, report_time, False)

    if check_only:
        logger.info("--check-only: skipping full ingestion (Dimension A gates 1-2 passed)")
        gates["check_only_mode"] = True
        return _build_report(gates, function_results, report_time, True)

    # ── Dimension A Gate 3: run all 7 functions ───────────────────────────────
    _nc_catalog: List[Dict] = []

    def get_nc_flow_refs():
        nonlocal _nc_catalog
        if not _nc_catalog:
            _nc_catalog = get_named_credentials(client)
        return get_named_credential_flow_refs(_nc_catalog, client)

    functions_to_run = [
        ("get_case_metrics",               lambda: get_case_metrics(client)),
        ("get_flow_inventory",             lambda: get_flow_inventory(client)),
        ("get_approval_pending",           lambda: get_approval_pending(client)),
        ("get_knowledge_coverage",         lambda: get_knowledge_coverage(client)),
        ("get_named_credentials",          lambda: get_named_credentials(client)),
        ("get_named_credential_flow_refs", get_nc_flow_refs),
        ("get_cross_system_references",    lambda: get_cross_system_references(client)),
    ]

    for fn_name, fn_call in functions_to_run:
        result = FunctionResult(fn_name)
        t0 = time.perf_counter()
        try:
            data = _with_retry(fn_name, fn_call, result)
            result.elapsed_ms = int((time.perf_counter() - t0) * 1000)
            result.data = data

            # Row count
            if isinstance(data, list):
                result.row_count = len(data)
            elif isinstance(data, dict):
                for count_key in [
                    "total_cases_90d", "active_flow_count_on_object",
                    "sf_total_cases", "pending_count",
                ]:
                    if count_key in data:
                        result.row_count = data[count_key]
                        break
                else:
                    result.row_count = len(data)

            # Shape check
            shape_ok, issues = _validate_shape(fn_name, data)
            result.shape_issues = issues

            if not shape_ok:
                result.status = "SHAPE_ERROR"
                logger.warning(f"[{fn_name}] Shape issues: {issues}")
            elif result.row_count == 0:
                result.status = "EMPTY"
                result.warnings.append(
                    "Returned empty dataset — valid in a blank org. "
                    "See seeding instructions to enable detector firing."
                )
            else:
                result.status = "OK"

        except IngestError as e:
            result.elapsed_ms = int((time.perf_counter() - t0) * 1000)
            result.status = "ERROR"
            result.error = str(e)

            # Tooling API specific hint
            if fn_name == "get_flow_inventory" and "Tooling" in str(e):
                result.error += (
                    " — Tooling API access required. "
                    "Check: Setup > Users > [user] > Profile > System Permissions "
                    "> 'API Enabled' and 'Modify Metadata Through Metadata API Functions'. "
                    "See AUTH_SETUP.md."
                )
        except Exception as e:
            result.elapsed_ms = int((time.perf_counter() - t0) * 1000)
            result.status = "ERROR"
            result.error = f"Unexpected error: {e}"

        function_results.append(result)
        logger.info(result.log_line())
        if result.warnings:
            for w in result.warnings:
                logger.warning(f"  \u26a0  [{fn_name}] {w}")

    # ── Dimension A gates: no hard failures, all shapes correct ───────────────
    gates["all_functions_succeed"] = all(
        not r.is_hard_failure for r in function_results
    )
    gates["all_shapes_correct"] = all(
        len(r.shape_issues) == 0 for r in function_results
    )

    # 3 key functions specifically (SF-3.1 spec)
    by_name = {r.name: r for r in function_results}
    gates["case_metrics_executed"]         = not by_name["get_case_metrics"].is_hard_failure
    gates["flow_inventory_executed"]       = not by_name["get_flow_inventory"].is_hard_failure
    gates["cross_system_refs_executed"]    = not by_name["get_cross_system_references"].is_hard_failure

    # ── Dimension B: volume — informational only ──────────────────────────────
    case_count = 0
    flow_count = 0
    cm = by_name.get("get_case_metrics")
    fi = by_name.get("get_flow_inventory")
    if cm and cm.data:
        case_count = cm.data.get("total_cases_90d", 0)
    if fi and fi.data:
        flow_count = fi.data.get("active_flow_count_on_object", 0)

    gates["volume_cases_sufficient"]  = case_count >= MIN_CASES_90D   # informational
    gates["volume_flows_sufficient"]  = flow_count >= MIN_ACTIVE_FLOWS # informational

    # ── SF-3.1 sprint passes on Dimension A only ──────────────────────────────
    dim_a_gates = [
        "credentials_present", "api_connected",
        "all_functions_succeed", "all_shapes_correct",
        "case_metrics_executed", "flow_inventory_executed",
        "cross_system_refs_executed",
    ]
    dim_a_passed = all(gates.get(g, False) for g in dim_a_gates)
    dim_b_passed = gates["volume_cases_sufficient"] and gates["volume_flows_sufficient"]

    seed_instructions = _build_seed_instructions(case_count, flow_count, gates)

    return _build_report(
        gates, function_results, report_time,
        passed=dim_a_passed,
        dim_a_passed=dim_a_passed,
        dim_b_passed=dim_b_passed,
        seed_instructions=seed_instructions,
    )


def _build_seed_instructions(
    case_count: int, flow_count: int, gates: Dict[str, bool]
) -> List[str]:
    instructions = []
    if not gates.get("volume_cases_sufficient"):
        instructions.append(
            f"Case volume: {case_count} cases in last 90 days (minimum {MIN_CASES_90D} for "
            f"meaningful D2/D4/D7 signal). Create {max(0, MIN_CASES_90D - case_count)} more Cases. "
            "Then reassign 20+ of them between users to generate CaseHistory owner-change records."
        )
    if not gates.get("volume_flows_sufficient"):
        instructions.append(
            "No active AutoLaunchedFlow on Case found. "
            "Create 2-3 AutoLaunchedFlows in Setup > Flows > New Flow > Record-Triggered "
            "(Case object, on Create). Even a single-assignment-element flow counts."
        )
    if not instructions:
        instructions.append("Org volume is sufficient. No seeding required before SF-3.2.")
    return instructions


def _build_report(
    gates: Dict[str, bool],
    function_results: List[FunctionResult],
    report_time: str,
    passed: bool,
    dim_a_passed: bool = False,
    dim_b_passed: bool = False,
    seed_instructions: Optional[List[str]] = None,
) -> Dict[str, Any]:
    return {
        "sf31_passed":   passed,       # True = sprint gate cleared (Dimension A only)
        "dim_a_passed":  dim_a_passed, # Connectivity & query correctness
        "dim_b_passed":  dim_b_passed, # Volume — informational
        "report_time":   report_time,
        "gates":         gates,
        "function_results": [r.to_dict() for r in function_results],
        "seed_instructions": seed_instructions or [],
        "summary": _build_summary(gates, function_results, passed, dim_b_passed),
    }


def _build_summary(
    gates: Dict[str, bool],
    function_results: List[FunctionResult],
    passed: bool,
    dim_b_passed: bool,
) -> str:
    n_ok    = sum(1 for r in function_results if r.status == "OK")
    n_empty = sum(1 for r in function_results if r.status == "EMPTY")
    n_err   = sum(1 for r in function_results if r.status in ("ERROR", "SHAPE_ERROR"))
    status  = "PASSED" if passed else "FAILED"
    vol_note = "" if dim_b_passed else " | \u26a0 Volume low \u2014 seed org before SF-3.2"
    return (
        f"SF-3.1 {status} | "
        f"Functions: {n_ok} \u2705 OK, {n_empty} \u26a0 empty (valid), {n_err} \u274c errors"
        f"{vol_note}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")

    parser = argparse.ArgumentParser(
        description="SF-3.1 Live Org Validator — AgentIQ discovery",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "See AUTH_SETUP.md for how to obtain SF credentials.\n\n"
            "Exit codes: 0 = Dimension A passed (sprint gate clear), "
            "1 = Dimension A failed (sprint gate blocked)"
        ),
    )
    parser.add_argument(
        "--check-only", action="store_true",
        help="Only test credentials and API connectivity; skip full ingestion"
    )
    parser.add_argument(
        "--report-path", default=None,
        help="Write JSON validation report to this path (required input for SF-3.2)"
    )
    args = parser.parse_args()

    report = run_validation(check_only=args.check_only)

    print()
    print("=" * 65)
    print(report["summary"])
    print("=" * 65)

    # Dimension A detail
    print()
    print("Dimension A \u2014 Connectivity & Query Correctness (sprint gate):")
    for fn_result in report["function_results"]:
        status_icon = {
            "OK":         "\u2705",
            "EMPTY":      "\u26a0 ",
            "SHAPE_ERROR":"\u274c",
            "ERROR":      "\u274c",
            "SKIPPED":    "\u23e9",
        }.get(fn_result["status"], "?")
        print(
            f"  {status_icon} {fn_result['function']:42s} "
            f"rows={fn_result['row_count']:<6} ms={fn_result['elapsed_ms']}"
        )
        if fn_result["error"]:
            print(f"      Error: {fn_result['error'][:100]}")
        if fn_result["warnings"]:
            for w in fn_result["warnings"]:
                print(f"      \u26a0 {w[:100]}")

    # Dimension B
    print()
    if report["dim_b_passed"]:
        print("Dimension B \u2014 Data Volume: \u2705 Sufficient for SF-3.2 calibration")
    else:
        print("Dimension B \u2014 Data Volume: \u26a0 Low (sprint still passes; seed before SF-3.2)")
        print()
        print("Seeding instructions:")
        for i, inst in enumerate(report["seed_instructions"], 1):
            print(f"  {i}. {inst}")

    # Failed Dimension A gates (if any)
    if not report["sf31_passed"]:
        print()
        print("Failed gates (must fix before SF-3.1 is complete):")
        dim_a = [
            "credentials_present", "api_connected",
            "all_functions_succeed", "all_shapes_correct",
            "case_metrics_executed", "flow_inventory_executed",
            "cross_system_refs_executed",
        ]
        for gate in dim_a:
            if not report["gates"].get(gate, True):
                print(f"  \u274c {gate}")

    if args.report_path:
        Path(args.report_path).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report_path).write_text(
            json.dumps(report, indent=2), encoding="utf-8"
        )
        print(f"\nReport written to {args.report_path}")
        print("Keep this report \u2014 it is required input for SF-3.2 calibration.")

    return 0 if report["sf31_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
