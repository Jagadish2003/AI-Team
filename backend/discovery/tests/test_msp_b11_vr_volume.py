"""MSP-B11 T6 / AT-701 — VR scan-cycle volume-coordination contract tests.

Proves a realistic ServiceNow scan cycle (thousands of vulnerable-item updates)
stays bounded and transparent through the reused MSP-B7 volume-control
foundations: within the calibrated budget it processes cleanly; over budget it
defers LOUDLY (never silent truncation) with a safe checkpoint and exact resume;
and aggregation stays at the workflow level — no host×vulnerability enumeration.
"""
from __future__ import annotations

import json
import os

import pytest

from discovery.ingest.secops_volume_harness import (
    gen_vulnerable_items,
    run_secops_volume_validation,
)
from discovery.signals.budget import BudgetReport
from discovery.signals.ops_calibration import CALIBRATED_RUN_EVENT_BUDGET
from discovery.signals.secops_volume import (
    TABLE_VULNERABLE_ITEM,
    SecOpsOrgScopeError,
    SecOpsVolumeStream,
)

_ITEMS = int(os.environ.get("MSP_B11_VR_VOLUME_ITEMS", "6000"))


@pytest.fixture(scope="module")
def report():
    return run_secops_volume_validation(
        "org_vr_ac7",
        vulnerable_items=_ITEMS,
        vulnerability_groups=250,
        remediation_tasks=750,
        estate_size=200,
    )


# ── within the calibrated budget ─────────────────────────────────────────────

def test_scan_cycle_burst_ingests_within_calibrated_budget(report):
    # A realistic scan cycle fits within the shared B7 budget — nothing deferred.
    assert report.budget == CALIBRATED_RUN_EVENT_BUDGET
    assert report.records_generated == _ITEMS + 250 + 750
    assert report.records_processed == report.records_generated
    assert report.records_deferred == 0
    assert report.records_rejected == 0
    assert report.envelope_pass, report.envelope_failures


def test_measurements_are_populated_not_assumed(report):
    assert report.records_seen == report.records_generated
    assert report.records_processed > 0
    assert report.aggregate_count > 0
    assert report.duration_seconds > 0 and report.records_per_sec > 0
    assert report.peak_memory_mb > 0
    assert report.batch_count > 1          # bounded batches, not one giant read
    assert report.max_batch_size <= 500
    # Every table family recorded a safe resume checkpoint.
    assert report.safe_checkpoints.get(TABLE_VULNERABLE_ITEM)


def test_aggregation_compresses_volume_and_stays_bounded(report):
    # Distinct workflow patterns « processed records — memory is bounded by
    # patterns (a scan re-finds the same estate), not by record volume.
    assert report.aggregate_count < report.records_processed
    assert report.aggregate_ratio <= report.envelope["max_aggregate_ratio"]
    assert report.peak_memory_mb <= report.envelope["max_peak_memory_mb"]


def test_no_host_vulnerability_enumeration_in_aggregates(report):
    # AC6 / workload-not-weakness: no aggregate carries a host, CVE, or per-item id.
    assert report.host_vuln_enumeration_detected is False
    rendered = json.dumps(report.workflow_aggregates)
    for forbidden in ("cve-", "CVE-", "vi-000", "vg-000", "rt-000", "cvss", "exploit", "scanner"):
        assert forbidden not in rendered
    # Aggregates carry only workflow classification + counts.
    for agg in report.workflow_aggregates:
        assert set(agg["workflow"]).issubset(
            {
                "vulnerability_class", "ci_class", "remediation_path",
                "lifecycle_state", "assignment_group", "category",
                "subcategory", "close_code",
            }
        )
        assert "sys_id" not in agg and "cmdb_ci" not in agg


# ── over budget: loud deferral, never silent truncation ──────────────────────

def test_budget_breach_defers_loudly_with_safe_checkpoint():
    r = run_secops_volume_validation(
        "org_vr_ac7", vulnerable_items=5000, vulnerability_groups=0,
        remediation_tasks=0, budget=1000,
    )
    assert r.records_processed == 1000
    assert r.records_deferred == 4000
    # Nothing is lost — seen == processed + deferred (+ rejected).
    assert r.records_seen == r.records_processed + r.records_deferred + r.records_rejected

    br = r.budget_report
    assert br["breached"] is True
    assert br["deferred"] == 4000
    assert br["reason"]                                   # human-readable, loud
    assert br["deferred_by_source"]["servicenow:sn_vul_vulnerable_item"] == 4000
    assert br["deferred_window"]["first"] and br["deferred_window"]["last"]
    # Safe checkpoint preserved for continuation (strictly before the deferred window).
    checkpoint = r.safe_checkpoints[TABLE_VULNERABLE_ITEM]
    assert checkpoint and checkpoint < br["deferred_window"]["first"]


def test_deferred_work_resumes_without_duplication_or_skip():
    r = run_secops_volume_validation(
        "org_vr_ac7", vulnerable_items=5000, vulnerability_groups=0,
        remediation_tasks=0, budget=1000,
    )
    assert r.resume_deferred_input == r.records_deferred == 4000
    assert r.resume_processed == 4000
    assert r.resume_duplicate_ids == 0
    assert r.resume_skipped_ids == 0


# ── stream-level: reuse, determinism, org-scope, malformed ───────────────────

def test_stream_reuses_the_b7_budget_primitive_not_an_independent_limit():
    # Default budget IS the B7-calibrated per-run budget (reused, not reinvented).
    stream = SecOpsVolumeStream()
    stream.admit(
        {"sys_id": "vi-1", "org_id": "o", "vulnerability_class": "Missing Patch",
         "state": "Open", "sys_updated_on": "2026-07-01 00:00:00"},
        table_family=TABLE_VULNERABLE_ITEM, org_id="o",
    )
    measurements = stream.measurements("o")
    assert measurements.budget == CALIBRATED_RUN_EVENT_BUDGET
    # The report is the reused BudgetReport shape.
    assert set(measurements.budget_report) == set(
        BudgetReport(budget=None, processed=0, deferred=0).to_dict()
    )


def test_folding_is_deterministic_under_reordering():
    items = gen_vulnerable_items(400, org_id="o", estate_size=50)
    forward = SecOpsVolumeStream(budget=None)
    reverse = SecOpsVolumeStream(budget=None)
    for rec in items:
        forward.admit(rec, table_family=TABLE_VULNERABLE_ITEM, org_id="o")
    for rec in reversed(items):
        reverse.admit(rec, table_family=TABLE_VULNERABLE_ITEM, org_id="o")
    assert [a.to_dict() for a in forward.workflow_aggregates("o")] == [
        a.to_dict() for a in reverse.workflow_aggregates("o")
    ]


def test_org_scoped_admission_refuses_a_foreign_org():
    stream = SecOpsVolumeStream(budget=None)
    with pytest.raises(SecOpsOrgScopeError):
        stream.admit(
            {"sys_id": "vi-1", "org_id": "org-b", "state": "Open"},
            table_family=TABLE_VULNERABLE_ITEM, org_id="org-a",
        )


def test_two_org_bursts_never_fold_together():
    stream = SecOpsVolumeStream(budget=None)
    for rec in gen_vulnerable_items(100, org_id="org-a", estate_size=20):
        stream.admit(rec, table_family=TABLE_VULNERABLE_ITEM, org_id="org-a")
    for rec in gen_vulnerable_items(100, org_id="org-b", estate_size=20):
        stream.admit(rec, table_family=TABLE_VULNERABLE_ITEM, org_id="org-b")
    a_aggs = stream.workflow_aggregates("org-a")
    b_aggs = stream.workflow_aggregates("org-b")
    assert a_aggs and b_aggs
    assert all(a.org_id == "org-a" for a in a_aggs)
    assert all(b.org_id == "org-b" for b in b_aggs)
    # An org only sees its own records counted.
    assert sum(a.item_count for a in a_aggs) == 100
    assert sum(b.item_count for b in b_aggs) == 100


def test_malformed_record_is_rejected_and_counted_not_processed():
    stream = SecOpsVolumeStream(budget=None)
    admission = stream.admit(
        {"org_id": "o", "state": "Open"},  # no sys_id
        table_family=TABLE_VULNERABLE_ITEM, org_id="o",
    )
    assert admission.is_rejected
    m = stream.measurements("o")
    assert m.records_rejected == 1
    assert m.records_processed == 0
    assert m.records_seen == 1


def test_unknown_table_family_fails_loudly():
    stream = SecOpsVolumeStream(budget=None)
    with pytest.raises(ValueError, match="unknown SecOps table family"):
        stream.admit({"sys_id": "x", "org_id": "o"}, table_family="not_a_table", org_id="o")


# ── report artifact (regenerates the calibration doc) ────────────────────────

def test_write_report_artifact_when_requested(report):
    """Write the measured report when MSP_B11_VR_VOLUME_REPORT_OUT is set.

    Regenerates ``docs/MSP-B11_VR_VOLUME_VALIDATION.md``:

        MSP_B11_VR_VOLUME_ITEMS=6000 \\
        MSP_B11_VR_VOLUME_REPORT_OUT=docs/MSP-B11_VR_VOLUME_VALIDATION.md \\
        python -m pytest discovery/tests/test_msp_b11_vr_volume.py

    A normal run (no env var) skips this test.
    """
    from discovery.ingest.secops_volume_harness import render_markdown

    out = os.environ.get("MSP_B11_VR_VOLUME_REPORT_OUT")
    if not out:
        pytest.skip("set MSP_B11_VR_VOLUME_REPORT_OUT to write the report artifact")
    env_label = os.environ.get("MSP_B11_VR_VOLUME_ENV", "unspecified")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(render_markdown(report, environment=env_label))
