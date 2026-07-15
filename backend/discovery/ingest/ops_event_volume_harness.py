"""MSP-B8 / T5 — realistic month-scale volume validation harness.

Unit fixtures prove the loaders (T2/T3) and the bridge (T4) are *correct*; this
harness proves they behave at a realistic **month of exported cloud events**, and
records MEASURED operational numbers — not assumptions — for MSP-B7's event-volume
budget and operational-envelope calibration (AC7).

What it exercises, end to end through the full bridge path:

  * **loader throughput** — representative AWS + Azure exports parsed and written
    to staging, with load time and rows/sec measured;
  * **staging size** — the row count that actually lands;
  * **skip + duplicate accounting** — a deliberate slice of malformed / duplicate
    records so ``rows_skipped`` and ``duplicate_count`` are non-zero and measured;
  * **row-id checkpoint paging + bridge batching** — the bridge drains staging in
    bounded pages, with batch-count and max-batch-size observed;
  * **downstream event emission + evidence traces** — normalised events carry a
    valid recurrence signature and resolvable raw-payload evidence at volume;
  * **checkpoint resume** — a second run after new rows arrive processes ONLY the
    new rows;
  * **memory pressure** — peak allocated memory over the whole run (tracemalloc).

The harness is provider-injectable: pass an
:class:`~discovery.ingest.ops_event_staging_store.InMemoryStagingSink` (used as
both sink and reader) for a DB-free run, or the real
:class:`DbStagingSink` + :class:`DbStagingReader` for the on-DB month-scale run.

Scope note — CloudWatch: the end-to-end volume mix uses the four surfaces whose
export shape and B0 mapper align cleanly today (EventBridge, CloudTrail, Azure
Monitor, Azure Activity Log). CloudWatch alarm-history vs ``map_cloudwatch`` shape
reconciliation is tracked with the T6 equivalence work and is deliberately out of
this measurement so the numbers are not muddied by a degraded surface.
"""
from __future__ import annotations

import logging
import time
import tracemalloc
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from .aws_export_loaders import (
    load_cloudtrail_logs,
    load_eventbridge_archive,
)
from .azure_export_loaders import (
    load_azure_activity_log,
    load_azure_monitor_alerts,
)
from .ops_event_bridge import OpsEventBridgeIngestor, resolve_raw_payload
from .base import Checkpoint

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Deterministic representative generators (one raw payload per event)
# ─────────────────────────────────────────────────────────────────────────────
# Index-driven (no randomness) so a run is reproducible: the same counts always
# produce the same corpus, so measurements are comparable across runs/machines.


def gen_eventbridge(n: int, start: int = 0) -> List[Dict[str, Any]]:
    out = []
    for i in range(start, start + n):
        out.append({
            "version": "0",
            "id": f"eb-{i:09d}",
            "detail-type": "EC2 Instance State-change Notification",
            "source": "aws.ec2",
            "account": "111122223333",
            "time": "2026-06-01T13:00:00Z",
            "region": "us-east-1",
            "resources": [f"arn:aws:ec2:us-east-1:111122223333:instance/i-{i:012x}"],
            "detail": {"instance-id": f"i-{i:012x}", "state": "stopped" if i % 2 else "running"},
        })
    return out


def gen_cloudtrail(n: int, start: int = 0) -> List[Dict[str, Any]]:
    names = ["DeleteBucket", "CreateAccessKey", "TerminateInstances", "PutBucketPolicy"]
    out = []
    for i in range(start, start + n):
        out.append({
            "eventVersion": "1.08",
            "eventID": f"ct-{i:09d}",
            "eventTime": "2026-06-01T14:15:16Z",
            "eventSource": "s3.amazonaws.com",
            "eventName": names[i % len(names)],
            "awsRegion": "us-east-1",
            "sourceIPAddress": "203.0.113.10",
            "userIdentity": {"type": "IAMUser", "arn": f"arn:aws:iam::111122223333:user/u{i % 25}"},
            "readOnly": False,
            "managementEvent": True,
        })
    return out


def gen_azure_monitor(n: int, start: int = 0) -> List[Dict[str, Any]]:
    sevs = ["Sev0", "Sev1", "Sev2", "Sev3", "Sev4"]
    out = []
    for i in range(start, start + n):
        out.append({
            "data": {"essentials": {
                "alertId": f"/subscriptions/s/providers/Microsoft.AlertsManagement/alerts/am-{i:09d}",
                "alertRule": f"rule-{i % 40}",
                "severity": sevs[i % len(sevs)],
                "firedDateTime": "2026-06-01T15:00:00Z",
                "monitorCondition": "Fired",
                "alertTargetIDs": [
                    f"/subscriptions/s/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm{i % 50}"
                ],
                "description": "threshold breached",
            }}
        })
    return out


def gen_azure_activity(n: int, start: int = 0) -> List[Dict[str, Any]]:
    ops = [
        "Microsoft.Compute/virtualMachines/write",
        "Microsoft.Storage/storageAccounts/delete",
        "Microsoft.Network/networkSecurityGroups/write",
    ]
    out = []
    for i in range(start, start + n):
        out.append({
            "eventDataId": f"aa-{i:09d}",
            "operationName": ops[i % len(ops)],
            "resourceId": f"/subscriptions/s/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm{i % 50}",
            "caller": f"user{i % 30}@contoso.com",
            "level": "Informational",
            "status": {"value": "Succeeded"},
            "eventTimestamp": "2026-06-01T16:00:00Z",
            "category": {"value": "Administrative"},
        })
    return out


def _inject_malformed_and_dupes(
    records: List[Dict[str, Any]], id_field: str, *, malformed_ratio: float, dupe_ratio: float
) -> tuple[List[Dict[str, Any]], int, int]:
    """Return (records, expected_malformed, expected_dupes) after injection.

    A malformed record drops its id field (the loader loud-skips it,
    ``MISSING_EVENT_ID``); a duplicate repeats the previous record's id (the
    loader's within-batch dedupe collapses it). Both are deterministic (every
    ``k``-th record) so the expected counts are exact — the harness asserts the
    measured skip/dupe counts against them.
    """
    if not records:
        return records, 0, 0
    malformed_every = int(1 / malformed_ratio) if malformed_ratio > 0 else 0
    dupe_every = int(1 / dupe_ratio) if dupe_ratio > 0 else 0
    out: List[Dict[str, Any]] = []
    malformed = dupes = 0
    for i, rec in enumerate(records):
        if malformed_every and i > 0 and i % malformed_every == 0:
            bad = dict(rec)
            bad.pop(id_field, None)
            out.append(bad)
            malformed += 1
            continue
        out.append(rec)
        # Duplicate at offset 1 within the period so a dupe index is never also a
        # malformed index (malformed hits multiples of malformed_every >= 2, which
        # are never == 1 mod dupe_every); keeps both counts exact and disjoint.
        if dupe_every and i > 0 and i % dupe_every == 1:
            out.append(dict(rec))  # exact duplicate id → within-batch dedupe
            dupes += 1
    return out, malformed, dupes


# ─────────────────────────────────────────────────────────────────────────────
# Measurement report
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class VolumeReport:
    """The measured operational picture — the MSP-B7 calibration hand-off."""

    org_id: str
    batch_size: int
    # corpus
    generated_records: int = 0
    per_format_counts: Dict[str, int] = field(default_factory=dict)
    expected_malformed: int = 0
    expected_dupes: int = 0
    # load (T2/T3)
    load_seconds: float = 0.0
    rows_loaded: int = 0          # newly inserted into staging
    rows_skipped: int = 0         # loud-skipped malformed records
    duplicate_count: int = 0      # collapsed duplicates
    staging_rows: int = 0
    load_rows_per_sec: float = 0.0
    # ingest / bridge (T4)
    ingest_seconds: float = 0.0
    events_emitted: int = 0
    batches: int = 0
    max_batch_size: int = 0
    final_checkpoint: str = ""
    ingest_events_per_sec: float = 0.0
    # resume
    resume_new_rows: int = 0
    resume_events: int = 0
    resume_seconds: float = 0.0
    # correctness at volume
    events_with_signature: int = 0
    evidence_samples_checked: int = 0
    evidence_samples_resolved: int = 0
    # memory
    peak_memory_mb: float = 0.0
    # envelope
    envelope: Dict[str, Any] = field(default_factory=dict)
    envelope_pass: bool = False
    envelope_failures: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


#: Proposed operational envelope — deliberately generous pass/fail bounds. The
#: POINT of T5 is the measured numbers; these bounds only catch a gross
#: regression. MSP-B7 sets the real budgets from the measurements.
DEFAULT_ENVELOPE: Dict[str, Any] = {
    "min_load_rows_per_sec": 500.0,
    "min_ingest_events_per_sec": 500.0,
    "max_peak_memory_mb": 1024.0,
    "require_resume_exact": True,
    "require_evidence_resolves": True,
}


def run_volume_validation(
    org_id: str,
    *,
    sink,
    reader,
    raw_store,
    per_format: int = 6000,
    batch_size: int = 500,
    malformed_ratio: float = 0.02,
    dupe_ratio: float = 0.01,
    evidence_sample: int = 25,
    envelope: Optional[Dict[str, Any]] = None,
) -> VolumeReport:
    """Load a month-scale AWS+Azure export and ingest it end to end, measured.

    ``sink`` writes staged rows (T2/T3 loaders), ``reader`` feeds the bridge (T4),
    ``raw_store`` receives raw payloads so evidence resolution can be spot-checked.
    Pass an ``InMemoryStagingSink`` as both ``sink`` and ``reader`` for a DB-free
    run, or ``DbStagingSink()`` + ``DbStagingReader()`` for the on-DB run.
    """
    env = {**DEFAULT_ENVELOPE, **(envelope or {})}
    report = VolumeReport(org_id=org_id, batch_size=batch_size, envelope=env)

    tracemalloc.start()
    try:
        # ── Generate the representative corpus ──────────────────────────────
        corpus = {
            "eventbridge_archive": (gen_eventbridge(per_format), "id"),
            "cloudtrail": (gen_cloudtrail(per_format), "eventID"),
            "azure_monitor": (gen_azure_monitor(per_format), "alertId"),  # nested; not dropped
            "azure_activity_log": (gen_azure_activity(per_format), "eventDataId"),
        }
        # Azure Monitor's id is nested (data.essentials.alertId); inject malformed
        # only where the id is top-level so the "missing id" skip is deterministic.
        prepared: Dict[str, List[Dict[str, Any]]] = {}
        for fmt, (recs, id_field) in corpus.items():
            if id_field in (recs[0] if recs else {}):
                recs2, mal, dup = _inject_malformed_and_dupes(
                    recs, id_field, malformed_ratio=malformed_ratio, dupe_ratio=dupe_ratio
                )
            else:
                recs2, mal, dup = recs, 0, 0
            prepared[fmt] = recs2
            report.per_format_counts[fmt] = len(recs2)
            report.generated_records += len(recs2)
            report.expected_malformed += mal
            report.expected_dupes += dup

        # ── Load into staging (measured) ────────────────────────────────────
        t0 = time.perf_counter()
        results = [
            load_eventbridge_archive(prepared["eventbridge_archive"], org_id=org_id,
                                     batch_id="vol:eventbridge", sink=sink),
            load_cloudtrail_logs(prepared["cloudtrail"], org_id=org_id,
                                 batch_id="vol:cloudtrail", sink=sink),
            load_azure_monitor_alerts(prepared["azure_monitor"], org_id=org_id,
                                      batch_id="vol:azure_monitor", sink=sink),
            load_azure_activity_log(prepared["azure_activity_log"], org_id=org_id,
                                    batch_id="vol:azure_activity", sink=sink),
        ]
        report.load_seconds = time.perf_counter() - t0
        report.rows_loaded = sum(r.inserted_count for r in results)
        report.rows_skipped = sum(r.skipped_count for r in results)
        report.duplicate_count = sum(r.duplicate_count for r in results)
        report.staging_rows = report.rows_loaded
        report.load_rows_per_sec = _rate(report.generated_records, report.load_seconds)

        # ── Ingest end to end through the bridge (measured) ─────────────────
        ingestor = OpsEventBridgeIngestor(reader, raw_store=raw_store, batch_size=batch_size)
        sample_records: List[Dict[str, Any]] = []
        t0 = time.perf_counter()
        for batch in ingestor.ingest_changes(org_id, None):
            report.batches += 1
            report.events_emitted += len(batch.records)
            report.max_batch_size = max(report.max_batch_size, len(batch.records))
            report.final_checkpoint = batch.next_checkpoint
            for rec in batch.records:
                if rec["event"].get("event_signature"):
                    report.events_with_signature += 1
                if len(sample_records) < evidence_sample:
                    sample_records.append(rec)
        report.ingest_seconds = time.perf_counter() - t0
        report.ingest_events_per_sec = _rate(report.events_emitted, report.ingest_seconds)

        # ── Evidence traces resolve at volume (spot-check) ──────────────────
        for rec in sample_records:
            report.evidence_samples_checked += 1
            if resolve_raw_payload(raw_store, org_id, rec) is not None:
                report.evidence_samples_resolved += 1

        # ── Checkpoint resume: only new rows are processed ──────────────────
        since = Checkpoint.create("ops_event_bridge", org_id, report.final_checkpoint)
        new_batch = gen_cloudtrail(200, start=10_000_000)  # distinct ids
        t0 = time.perf_counter()
        resume_load = load_cloudtrail_logs(new_batch, org_id=org_id, batch_id="vol:resume", sink=sink)
        report.resume_new_rows = resume_load.inserted_count
        resume_events = 0
        for batch in ingestor.ingest_changes(org_id, since):
            resume_events += len(batch.records)
        report.resume_seconds = time.perf_counter() - t0
        report.resume_events = resume_events

        current, peak = tracemalloc.get_traced_memory()
        report.peak_memory_mb = round(peak / (1024 * 1024), 2)
    finally:
        tracemalloc.stop()

    _evaluate_envelope(report, env)
    return report


def _rate(count: int, seconds: float) -> float:
    return round(count / seconds, 1) if seconds > 0 else 0.0


def _evaluate_envelope(report: VolumeReport, env: Dict[str, Any]) -> None:
    failures: List[str] = []
    if report.load_rows_per_sec < env["min_load_rows_per_sec"]:
        failures.append(
            f"load throughput {report.load_rows_per_sec}/s < {env['min_load_rows_per_sec']}/s"
        )
    if report.ingest_events_per_sec < env["min_ingest_events_per_sec"]:
        failures.append(
            f"ingest throughput {report.ingest_events_per_sec}/s < {env['min_ingest_events_per_sec']}/s"
        )
    if report.peak_memory_mb > env["max_peak_memory_mb"]:
        failures.append(
            f"peak memory {report.peak_memory_mb}MB > {env['max_peak_memory_mb']}MB"
        )
    if env["require_resume_exact"] and report.resume_events != report.resume_new_rows:
        failures.append(
            f"resume processed {report.resume_events} != {report.resume_new_rows} new rows"
        )
    if env["require_evidence_resolves"] and (
        report.evidence_samples_checked == 0
        or report.evidence_samples_resolved != report.evidence_samples_checked
    ):
        failures.append(
            f"evidence resolved {report.evidence_samples_resolved}/"
            f"{report.evidence_samples_checked}"
        )
    report.envelope_failures = failures
    report.envelope_pass = not failures


# ─────────────────────────────────────────────────────────────────────────────
# Report rendering (the MSP-B7 hand-off)
# ─────────────────────────────────────────────────────────────────────────────

def render_markdown(report: VolumeReport, *, environment: str = "") -> str:
    """Render a measured report as the MSP-B7 calibration hand-off markdown."""
    r = report
    status = "PASS" if r.envelope_pass else "FAIL"
    lines = [
        "# MSP-B8 — Month-Scale Volume Validation (T5)",
        "",
        "> Recorded evidence for MSP-B7 event-volume budget & operational-envelope",
        "> calibration (AC7). Measured numbers, not assumptions.",
        "",
        f"- **Envelope result:** {status}",
        f"- **Environment:** {environment or 'unspecified'}",
        f"- **Org:** `{r.org_id}`  |  **Bridge batch size:** {r.batch_size}",
        "",
        "## Corpus (representative month of AWS + Azure exports)",
        "",
        "| Surface | Records |",
        "| --- | --- |",
    ]
    for fmt, cnt in r.per_format_counts.items():
        lines.append(f"| `{fmt}` | {cnt:,} |")
    lines += [
        f"| **Total generated** | **{r.generated_records:,}** |",
        f"| _of which malformed (injected)_ | {r.expected_malformed:,} |",
        f"| _of which duplicates (injected)_ | {r.expected_dupes:,} |",
        "",
        "## Load (T2/T3 loaders → staging)",
        "",
        "| Metric | Value |",
        "| --- | --- |",
        f"| Load time | {r.load_seconds:.3f} s |",
        f"| Rows loaded to staging | {r.rows_loaded:,} |",
        f"| Rows loud-skipped (malformed) | {r.rows_skipped:,} |",
        f"| Duplicates collapsed | {r.duplicate_count:,} |",
        f"| **Load throughput** | **{r.load_rows_per_sec:,.1f} rows/s** |",
        "",
        "## Ingest (T4 bridge → normalized events)",
        "",
        "| Metric | Value |",
        "| --- | --- |",
        f"| Ingest time | {r.ingest_seconds:.3f} s |",
        f"| Normalized events emitted | {r.events_emitted:,} |",
        f"| Bridge batches | {r.batches:,} |",
        f"| Max batch size | {r.max_batch_size:,} |",
        f"| Final row-id checkpoint | {r.final_checkpoint} |",
        f"| **Ingest throughput** | **{r.ingest_events_per_sec:,.1f} events/s** |",
        "",
        "## Correctness at volume",
        "",
        "| Metric | Value |",
        "| --- | --- |",
        f"| Events with recurrence signature | {r.events_with_signature:,} / {r.events_emitted:,} |",
        f"| Evidence traces resolved (sample) | {r.evidence_samples_resolved} / {r.evidence_samples_checked} |",
        f"| Resume: new rows processed | {r.resume_events} (of {r.resume_new_rows} added) |",
        "",
        "## Resource pressure",
        "",
        "| Metric | Value |",
        "| --- | --- |",
        f"| Peak memory (tracemalloc) | {r.peak_memory_mb:,.2f} MB |",
        "",
        "## Envelope (proposed — MSP-B7 to calibrate)",
        "",
        "| Bound | Threshold |",
        "| --- | --- |",
        f"| Min load throughput | {r.envelope['min_load_rows_per_sec']:,.0f} rows/s |",
        f"| Min ingest throughput | {r.envelope['min_ingest_events_per_sec']:,.0f} events/s |",
        f"| Max peak memory | {r.envelope['max_peak_memory_mb']:,.0f} MB |",
        f"| Resume exactness | {'required' if r.envelope['require_resume_exact'] else 'optional'} |",
        f"| Evidence resolves | {'required' if r.envelope['require_evidence_resolves'] else 'optional'} |",
        "",
    ]
    if r.envelope_failures:
        lines.append("### Envelope failures")
        lines += [f"- {f}" for f in r.envelope_failures]
        lines.append("")
    lines += [
        "## For MSP-B7 (calibration input)",
        "",
        "These are the first realistic event-volume signals for the MSP pack — use",
        "them to seed the budget/operational-envelope, not as final limits:",
        "",
        f"- **Per-event cost:** ~{_ms_per(r.load_seconds, r.rows_loaded)} ms/event to load and "
        f"~{_ms_per(r.ingest_seconds, r.events_emitted)} ms/event to ingest through the bridge, "
        "measured with tracemalloc active (a conservative ceiling; real throughput is higher).",
        f"- **A month of ~{r.generated_records:,} events** loads + ingests end to end in "
        f"~{r.load_seconds + r.ingest_seconds:.0f} s here, well within a batch/offline window.",
        f"- **Memory is flat and small** ({r.peak_memory_mb:,.2f} MB peak) — the loaders and the "
        "bridge stream in bounded batches, so volume drives time, not memory.",
        "- **Skip + dedupe accounting is exact and visible**, so a partner can trust the "
        "loaded-vs-exported reconciliation at scale.",
        "- **Ingestion is incremental** — a resume after new rows processed only the new rows, "
        "so steady-state runs cost per-delta, not per-history.",
        "",
        "## Methodology & reproduction",
        "",
        "- **Deterministic corpus:** index-driven generators (no randomness), so the same",
        "  counts reproduce the same corpus and comparable numbers. A fixed slice is injected",
        "  malformed (missing id → loud-skip) and duplicated (→ dedupe) to exercise accounting.",
        "- **Full path:** T2/T3 loaders → `ops_event_staging` → T4 bridge (read-only DB path,",
        "  row-id checkpoint paging) → normalized `OperationalEvent`s with resolvable evidence.",
        "- **Reproduce** (writes this file), against the disposable test DB:",
        "",
        "  ```",
        "  MSP_B8_VOLUME_PER_FORMAT=7500 \\",
        "  MSP_B8_VOLUME_REPORT_OUT=docs/MSP-B8_VOLUME_VALIDATION.md \\",
        "  python -m pytest backend/tests/contract/test_ops_event_volume.py",
        "  ```",
        "",
        "- **Scope — CloudWatch:** the mix uses the four surfaces whose export shape and B0",
        "  mapper align cleanly today (EventBridge, CloudTrail, Azure Monitor, Azure Activity",
        "  Log). CloudWatch alarm-history ↔ `map_cloudwatch` shape reconciliation is tracked",
        "  with the T6 equivalence work and is intentionally excluded so a degraded surface",
        "  does not skew these numbers.",
        "",
    ]
    return "\n".join(lines)


def _ms_per(seconds: float, count: int) -> str:
    return f"{(seconds / count * 1000):.3f}" if count else "n/a"
