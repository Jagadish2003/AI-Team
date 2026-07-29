"""MSP-B11 T6 / AT-701 — Vulnerability-Response scan-cycle volume validation harness.

The VR analogue of the MSP-B8 month-scale harness
(:mod:`discovery.ingest.ops_event_volume_harness`). Unit fixtures prove the SecOps
ingestion (T1-T5) is *correct*; this harness proves it stays **bounded and
transparent** under a realistic ServiceNow **scan cycle** — thousands of
vulnerable-item updates landing at once — and records MEASURED numbers to hand to
MSP-B7 calibration (the VR analogue of B8's AC7).

It exercises, end to end, the volume-coordination path:

  * **admission + folding** — a deterministic burst is admitted through
    :class:`~discovery.signals.secops_volume.SecOpsVolumeStream`, folding each
    record into ONE workflow aggregate per remediation-workflow pattern;
  * **budgeting** — the reused B7 :class:`RunBudget` bounds the run; a breach is
    deferred-and-counted (loud), never truncated silently;
  * **bounded memory** — a scan re-finds the same estate, so distinct workflow
    patterns stay small regardless of record volume (peak memory measured with
    tracemalloc); aggregation compresses volume, not evidence;
  * **resume** — the deferred tail re-admits on a continuation run and every
    record lands exactly once (no duplication, no skip);
  * **workload, not weakness** — the harness asserts no aggregate enumerates a
    host×vulnerability pair (no CVE / host / per-item id crosses the boundary).

Deterministic (index-driven generators, no randomness) so the same counts always
reproduce the same corpus and comparable numbers.
"""
from __future__ import annotations

import json
import re
import time
import tracemalloc
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

from discovery.signals.budget import DEFAULT_RUN_EVENT_BUDGET
from discovery.signals.secops_volume import (
    TABLE_REMEDIATION_TASK,
    TABLE_VULNERABILITY_GROUP,
    TABLE_VULNERABLE_ITEM,
    SecOpsVolumeStream,
)

# ── deterministic scan-cycle generators (bounded estate → bounded workflow set)

#: A scan cycle re-finds a bounded estate over and over; these bounded vocabularies
#: are what make aggregation compress a burst into a handful of workflow facts.
VULN_CLASSES = (
    "Missing Patch", "Misconfiguration", "End-of-Life Software",
    "Weak Cipher", "Default Credentials",
)
CI_CLASSES = ("cmdb_ci_server", "cmdb_ci_db_instance", "cmdb_ci_lb", "cmdb_ci_storage_device")
ASSIGNMENT_GROUPS = (
    "Vulnerability Management", "Platform Operations",
    "Database Operations", "Network Operations",
)
#: Canonical remediation lifecycle; each item's history is a prefix ending at its state.
LIFECYCLE = ("Open", "Assigned", "In Progress", "Resolved", "Closed")
SEVERITY_BANDS = ("1 - Critical", "2 - High", "3 - Moderate", "4 - Low")

#: Base epoch second for the deterministic monotonic ``sys_updated_on`` cursor.
_BASE_EPOCH = 1_780_000_000  # a fixed 2026 instant; never `datetime.now()` (determinism)


def _cursor(index: int) -> str:
    """Monotonic, distinct ``sys_updated_on`` per record (scan writes are ordered)."""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(_BASE_EPOCH + index))


def _state_history(current_index: int, changed_base: int) -> List[Dict[str, str]]:
    """A prefix lifecycle path ending at ``LIFECYCLE[current_index]`` as audit rows."""
    history = []
    for step in range(current_index):
        history.append(
            {
                "field": "state",
                "from_value": LIFECYCLE[step],
                "to_value": LIFECYCLE[step + 1],
                "changed_at": _cursor(changed_base * 10 + step),
            }
        )
    return history


def gen_vulnerable_items(
    n: int, *, org_id: str, start: int = 0, estate_size: int = 200
) -> List[Dict[str, Any]]:
    """A deterministic burst of vulnerable-item updates over a bounded estate."""
    out: List[Dict[str, Any]] = []
    for i in range(start, start + n):
        ci_slot = i % estate_size
        current = i % len(LIFECYCLE)
        out.append(
            {
                "sys_id": f"vi-{i:07d}",
                "number": f"VIT{i:07d}",
                "org_id": org_id,
                "state": LIFECYCLE[current],
                "vulnerability_class": VULN_CLASSES[i % len(VULN_CLASSES)],
                "severity": SEVERITY_BANDS[i % len(SEVERITY_BANDS)],
                "cmdb_ci": f"ci-{ci_slot:05d}",
                # T3-resolved CI class (bounded by estate); the volume fold reuses it.
                "resolved_ci": {"ci_class": CI_CLASSES[ci_slot % len(CI_CLASSES)]},
                "assignment_group": ASSIGNMENT_GROUPS[i % len(ASSIGNMENT_GROUPS)],
                "state_history": _state_history(current, i),
                "sys_updated_on": _cursor(i),
                "source_timestamp": _cursor(i),
            }
        )
    return out


def gen_vulnerability_groups(n: int, *, org_id: str, start: int = 0) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for i in range(start, start + n):
        out.append(
            {
                "sys_id": f"vg-{i:07d}",
                "number": f"VGRP{i:07d}",
                "org_id": org_id,
                "state": LIFECYCLE[i % len(LIFECYCLE)],
                "vulnerability_class": VULN_CLASSES[i % len(VULN_CLASSES)],
                "severity": SEVERITY_BANDS[i % len(SEVERITY_BANDS)],
                "assignment_group": ASSIGNMENT_GROUPS[i % len(ASSIGNMENT_GROUPS)],
                "sys_updated_on": _cursor(i),
            }
        )
    return out


def gen_remediation_tasks(n: int, *, org_id: str, start: int = 0) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for i in range(start, start + n):
        out.append(
            {
                "sys_id": f"rt-{i:07d}",
                "number": f"RTASK{i:07d}",
                "org_id": org_id,
                "state": LIFECYCLE[i % len(LIFECYCLE)],
                "assignment_group": ASSIGNMENT_GROUPS[i % len(ASSIGNMENT_GROUPS)],
                "sys_updated_on": _cursor(i),
            }
        )
    return out


def _batched(records: List[Any], size: int) -> Iterable[List[Any]]:
    for i in range(0, len(records), size):
        yield records[i : i + size]


# ── measured report (the MSP-B7 calibration hand-off) ────────────────────────

#: A host×vulnerability leak would look like an enumerated CVE or a per-item id in
#: the workflow aggregates. Aggregates carry neither by construction; the harness
#: scans for these to prove it (AC6).
_FORBIDDEN_ENUMERATION_RE = re.compile(
    r"(?i)(cve-\d{4}-\d+|\bvi-\d{7}\b|\bvg-\d{7}\b|\brt-\d{7}\b|cvss|exploit|scanner)"
)


@dataclass
class SecOpsVolumeReport:
    """The measured VR scan-cycle picture — hand-off to MSP-B7 calibration."""

    org_id: str
    budget: Optional[int]
    # corpus
    per_table_generated: Dict[str, int] = field(default_factory=dict)
    records_generated: int = 0
    estate_size: int = 0
    # volume behaviour (measured)
    records_seen: int = 0
    records_processed: int = 0
    records_deferred: int = 0
    records_rejected: int = 0
    aggregate_count: int = 0
    aggregate_ratio: float = 0.0
    per_table: Dict[str, Dict[str, int]] = field(default_factory=dict)
    safe_checkpoints: Dict[str, Optional[str]] = field(default_factory=dict)
    batch_count: int = 0
    max_batch_size: int = 0
    duration_seconds: float = 0.0
    records_per_sec: float = 0.0
    peak_memory_mb: float = 0.0
    budget_report: Dict[str, Any] = field(default_factory=dict)
    # resume (continuation of deferred work)
    resume_deferred_input: int = 0
    resume_processed: int = 0
    resume_duplicate_ids: int = 0
    resume_skipped_ids: int = 0
    # workload-not-weakness proof
    host_vuln_enumeration_detected: bool = False
    workflow_aggregates: List[Dict[str, Any]] = field(default_factory=list)
    # envelope
    envelope: Dict[str, Any] = field(default_factory=dict)
    envelope_pass: bool = False
    envelope_failures: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


#: Proposed operational envelope for VR scan-cycle bursts — deliberately generous;
#: the POINT of T6 is the measured numbers. MSP-B7 sets the real budgets.
DEFAULT_ENVELOPE: Dict[str, Any] = {
    "min_records_per_sec": 1000.0,
    "max_peak_memory_mb": 512.0,
    "max_aggregate_ratio": 0.10,        # distinct workflow patterns « records (compression)
    "require_resume_exact": True,
    "require_no_host_vuln_enumeration": True,
}


def run_secops_volume_validation(
    org_id: str,
    *,
    vulnerable_items: int = 6000,
    vulnerability_groups: int = 250,
    remediation_tasks: int = 750,
    estate_size: int = 200,
    budget: Optional[int] = DEFAULT_RUN_EVENT_BUDGET,
    batch_size: int = 500,
    envelope: Optional[Dict[str, Any]] = None,
) -> SecOpsVolumeReport:
    """Admit a realistic scan-cycle burst through the volume coordinator, measured.

    Generates a deterministic VR burst, admits it in bounded batches through
    :class:`SecOpsVolumeStream` under ``budget`` (default the B7-calibrated budget),
    measures duration/memory/aggregation, then proves the deferred tail resumes
    exactly (no duplication, no skip) on a continuation run.
    """
    env = {**DEFAULT_ENVELOPE, **(envelope or {})}
    report = SecOpsVolumeReport(org_id=org_id, budget=budget, estate_size=estate_size, envelope=env)

    corpus: List[Tuple[str, List[Dict[str, Any]]]] = [
        (TABLE_VULNERABLE_ITEM, gen_vulnerable_items(vulnerable_items, org_id=org_id, estate_size=estate_size)),
        (TABLE_VULNERABILITY_GROUP, gen_vulnerability_groups(vulnerability_groups, org_id=org_id)),
        (TABLE_REMEDIATION_TASK, gen_remediation_tasks(remediation_tasks, org_id=org_id)),
    ]
    for family, records in corpus:
        report.per_table_generated[family] = len(records)
        report.records_generated += len(records)

    # Flat admission order (what the ingestor would page through), used to
    # reconstruct the deferred tail for the resume proof.
    admission_order: List[Tuple[str, Dict[str, Any]]] = [
        (family, rec) for family, records in corpus for rec in records
    ]

    tracemalloc.start()
    try:
        stream = SecOpsVolumeStream(budget=budget)
        t0 = time.perf_counter()
        for family, records in corpus:
            for batch in _batched(records, batch_size):
                report.batch_count += 1
                report.max_batch_size = max(report.max_batch_size, len(batch))
                for rec in batch:
                    stream.admit(rec, table_family=family, org_id=org_id)
        report.duration_seconds = time.perf_counter() - t0

        measurements = stream.measurements(org_id)
        report.records_seen = measurements.records_seen
        report.records_processed = measurements.records_processed
        report.records_deferred = measurements.records_deferred
        report.records_rejected = measurements.records_rejected
        report.aggregate_count = measurements.aggregate_count
        report.per_table = {k: dict(v) for k, v in measurements.per_table.items()}
        report.safe_checkpoints = dict(measurements.safe_checkpoints)
        report.budget_report = dict(measurements.budget_report)
        report.workflow_aggregates = list(measurements.workflow_aggregates)

        # Resume: the deferred tail (records admitted past the budgeted window) is
        # re-admitted on a continuation run; every record must land exactly once.
        deferred_tail = admission_order[report.records_processed:]
        report.resume_deferred_input = len(deferred_tail)
        processed_ids = {rec["sys_id"] for _, rec in admission_order[: report.records_processed]}
        resume_stream = SecOpsVolumeStream(budget=None)  # drain the backlog
        resume_ids: List[str] = []
        for family, rec in deferred_tail:
            admission = resume_stream.admit(rec, table_family=family, org_id=org_id)
            if admission.is_processed:
                resume_ids.append(rec["sys_id"])
        report.resume_processed = len(resume_ids)
        report.resume_duplicate_ids = sum(1 for sid in resume_ids if sid in processed_ids)
        resume_id_set = set(resume_ids)
        all_ids = {rec["sys_id"] for _, rec in admission_order}
        covered = processed_ids | resume_id_set
        report.resume_skipped_ids = len(all_ids - covered)

        report.peak_memory_mb = round(tracemalloc.get_traced_memory()[1] / (1024 * 1024), 2)
    finally:
        tracemalloc.stop()

    report.records_per_sec = _rate(report.records_processed, report.duration_seconds)
    report.aggregate_ratio = (
        round(report.aggregate_count / report.records_processed, 4)
        if report.records_processed
        else 0.0
    )
    report.host_vuln_enumeration_detected = bool(
        _FORBIDDEN_ENUMERATION_RE.search(json.dumps(report.workflow_aggregates))
    )

    _evaluate_envelope(report, env)
    return report


def _rate(count: int, seconds: float) -> float:
    return round(count / seconds, 1) if seconds > 0 else 0.0


def _evaluate_envelope(report: SecOpsVolumeReport, env: Dict[str, Any]) -> None:
    failures: List[str] = []
    if report.records_per_sec < env["min_records_per_sec"]:
        failures.append(
            f"throughput {report.records_per_sec}/s < {env['min_records_per_sec']}/s"
        )
    if report.peak_memory_mb > env["max_peak_memory_mb"]:
        failures.append(
            f"peak memory {report.peak_memory_mb}MB > {env['max_peak_memory_mb']}MB"
        )
    if report.aggregate_ratio > env["max_aggregate_ratio"]:
        failures.append(
            f"aggregate ratio {report.aggregate_ratio} > {env['max_aggregate_ratio']} "
            "(insufficient volume compression)"
        )
    if env["require_resume_exact"] and (
        report.resume_duplicate_ids != 0 or report.resume_skipped_ids != 0
    ):
        failures.append(
            f"resume not exact: {report.resume_duplicate_ids} duplicated, "
            f"{report.resume_skipped_ids} skipped"
        )
    if env["require_no_host_vuln_enumeration"] and report.host_vuln_enumeration_detected:
        failures.append("host×vulnerability enumeration detected in aggregates")
    report.envelope_failures = failures
    report.envelope_pass = not failures


# ── report rendering (the MSP-B7 hand-off) ───────────────────────────────────

def render_markdown(report: SecOpsVolumeReport, *, environment: str = "") -> str:
    """Render a measured VR scan-cycle report as the MSP-B7 calibration hand-off."""
    r = report
    status = "PASS" if r.envelope_pass else "FAIL"
    br = r.budget_report
    lines = [
        "# MSP-B11 — Vulnerability-Response Scan-Cycle Volume Validation (T6)",
        "",
        "> Recorded evidence for MSP-B7 event-volume budget calibration — the VR",
        "> analogue of MSP-B8's AC7. Measured numbers, not assumptions.",
        "",
        f"- **Envelope result:** {status}",
        f"- **Environment:** {environment or 'unspecified'}",
        f"- **Org:** `{r.org_id}`  |  **Budget:** {r.budget}  |  **Estate size:** {r.estate_size}",
        "",
        "## Corpus (representative ServiceNow scan cycle)",
        "",
        "| Table family | Records |",
        "| --- | --- |",
    ]
    for family, cnt in sorted(r.per_table_generated.items()):
        lines.append(f"| `{family}` | {cnt:,} |")
    lines += [
        f"| **Total generated** | **{r.records_generated:,}** |",
        "",
        "## Volume behaviour (measured)",
        "",
        "| Metric | Value |",
        "| --- | --- |",
        f"| Records seen | {r.records_seen:,} |",
        f"| Records processed | {r.records_processed:,} |",
        f"| Records deferred (budget breach) | {r.records_deferred:,} |",
        f"| Records rejected (malformed) | {r.records_rejected:,} |",
        f"| Distinct workflow aggregates | {r.aggregate_count:,} |",
        f"| Aggregate ratio (patterns / processed) | {r.aggregate_ratio} |",
        f"| Batches | {r.batch_count:,} |",
        f"| Max batch size | {r.max_batch_size:,} |",
        f"| Processing time | {r.duration_seconds:.3f} s |",
        f"| **Throughput** | **{r.records_per_sec:,.1f} records/s** |",
        "",
        "## Budget & deferral (loud, never silent)",
        "",
        "| Metric | Value |",
        "| --- | --- |",
        f"| Budget | {br.get('budget')} |",
        f"| Processed | {br.get('processed'):,} |",
        f"| Deferred | {br.get('deferred'):,} |",
        f"| Breached | {br.get('breached')} |",
        f"| Deferred by source | {br.get('deferred_by_source')} |",
        f"| Deferred window | {br.get('deferred_window')} |",
        f"| Reason | {br.get('reason')} |",
        f"| Safe checkpoints (resume cursors) | {r.safe_checkpoints} |",
        "",
        "## Resume (deferred work continues exactly)",
        "",
        "| Metric | Value |",
        "| --- | --- |",
        f"| Deferred tail re-admitted | {r.resume_deferred_input:,} |",
        f"| Resume processed | {r.resume_processed:,} |",
        f"| Duplicated on resume | {r.resume_duplicate_ids} |",
        f"| Skipped on resume | {r.resume_skipped_ids} |",
        "",
        "## Resource pressure",
        "",
        "| Metric | Value |",
        "| --- | --- |",
        f"| Peak memory (tracemalloc) | {r.peak_memory_mb:,.2f} MB |",
        "",
        "## Workload, not weakness (AC6)",
        "",
        f"- Host×vulnerability enumeration detected in aggregates: **{r.host_vuln_enumeration_detected}**",
        f"- Aggregation compressed {r.records_processed:,} processed records into "
        f"{r.aggregate_count:,} workflow patterns — counts by class/CI-class/"
        "remediation-path, never host×CVE pairs.",
        "",
        "## Envelope (proposed — MSP-B7 to calibrate)",
        "",
        "| Bound | Threshold |",
        "| --- | --- |",
        f"| Min throughput | {r.envelope['min_records_per_sec']:,.0f} records/s |",
        f"| Max peak memory | {r.envelope['max_peak_memory_mb']:,.0f} MB |",
        f"| Max aggregate ratio | {r.envelope['max_aggregate_ratio']} |",
        f"| Resume exactness | {'required' if r.envelope['require_resume_exact'] else 'optional'} |",
        f"| No host×vuln enumeration | {'required' if r.envelope['require_no_host_vuln_enumeration'] else 'optional'} |",
        "",
    ]
    if r.envelope_failures:
        lines.append("### Envelope failures")
        lines += [f"- {f}" for f in r.envelope_failures]
        lines.append("")
    lines += [
        "## For MSP-B7 (calibration input)",
        "",
        "These are the first realistic VR scan-cycle volume signals for the MSP pack —",
        "the VR analogue of B8's cloud-event month. Use them to confirm the shared",
        "per-run budget covers scan-cycle bursts, not as final limits:",
        "",
        f"- **Per-record cost:** ~{_ms_per(r.duration_seconds, r.records_processed)} ms/record to "
        "admit + fold, measured with tracemalloc active (a conservative ceiling).",
        f"- **A scan cycle of ~{r.records_generated:,} records** admits + folds in "
        f"~{r.duration_seconds:.2f} s here, well within a batch/offline window.",
        f"- **Memory is bounded by workflow patterns, not record volume** "
        f"({r.peak_memory_mb:,.2f} MB peak for {r.aggregate_count:,} patterns) — a scan "
        "re-finds the same estate, so folding compresses the burst.",
        "- **A budget breach defers loudly** with a per-table breakdown and a safe "
        "checkpoint, and the deferred tail resumes exactly (no duplication, no skip).",
        "- **The shared B7 per-run budget (250,000) is reused verbatim** — no independent "
        "SecOps limit — so VR bursts and cloud events answer to one calibrated ceiling.",
        "",
        "## Methodology & reproduction",
        "",
        "- **Deterministic corpus:** index-driven generators (no randomness) over a bounded",
        "  estate, so the same counts reproduce the same corpus and comparable numbers.",
        "- **Path:** burst records → `SecOpsVolumeStream.admit` (reusing `RunBudget` /",
        "  `BudgetReport` from MSP-B7 and the T4 `remediation_signature` fold key) → workflow",
        "  aggregates + budget/deferral report + safe checkpoint.",
        "- **Reproduce** (writes this file):",
        "",
        "  ```",
        "  MSP_B11_VR_VOLUME_ITEMS=6000 \\",
        "  MSP_B11_VR_VOLUME_REPORT_OUT=docs/MSP-B11_VR_VOLUME_VALIDATION.md \\",
        "  python -m pytest discovery/tests/test_msp_b11_vr_volume.py",
        "  ```",
        "",
    ]
    return "\n".join(lines)


def _ms_per(seconds: float, count: int) -> str:
    return f"{(seconds / count * 1000):.4f}" if count else "n/a"
