# MSP-B11 — Vulnerability-Response Scan-Cycle Volume Validation (T6)

> Recorded evidence for MSP-B7 event-volume budget calibration — the VR
> analogue of MSP-B8's AC7. Measured numbers, not assumptions.

- **Envelope result:** PASS
- **Environment:** Windows 11, Python 3.11.9, single process, tracemalloc active (timings conservative)
- **Org:** `org_vr_ac7`  |  **Budget:** 250000  |  **Estate size:** 200

## Corpus (representative ServiceNow scan cycle)

| Table family | Records |
| --- | --- |
| `sn_vul_remediation_task` | 750 |
| `sn_vul_vulnerability_group` | 250 |
| `sn_vul_vulnerable_item` | 6,000 |
| **Total generated** | **7,000** |

## Volume behaviour (measured)

| Metric | Value |
| --- | --- |
| Records seen | 7,000 |
| Records processed | 7,000 |
| Records deferred (budget breach) | 0 |
| Records rejected (malformed) | 0 |
| Distinct workflow aggregates | 60 |
| Aggregate ratio (patterns / processed) | 0.0086 |
| Batches | 15 |
| Max batch size | 500 |
| Processing time | 2.208 s |
| **Throughput** | **3,170.6 records/s** |

## Budget & deferral (loud, never silent)

| Metric | Value |
| --- | --- |
| Budget | 250000 |
| Processed | 7,000 |
| Deferred | 0 |
| Breached | False |
| Deferred by source | {} |
| Deferred window | None |
| Reason | None |
| Safe checkpoints (resume cursors) | {'sn_vul_vulnerable_item': '2026-05-28 22:06:39', 'sn_vul_vulnerability_group': '2026-05-28 20:30:49', 'sn_vul_remediation_task': '2026-05-28 20:39:09'} |

## Resume (deferred work continues exactly)

| Metric | Value |
| --- | --- |
| Deferred tail re-admitted | 0 |
| Resume processed | 0 |
| Duplicated on resume | 0 |
| Skipped on resume | 0 |

## Resource pressure

| Metric | Value |
| --- | --- |
| Peak memory (tracemalloc) | 1.43 MB |

## Workload, not weakness (AC6)

- Host×vulnerability enumeration detected in aggregates: **False**
- Aggregation compressed 7,000 processed records into 60 workflow patterns — counts by class/CI-class/remediation-path, never host×CVE pairs.

## Envelope (proposed — MSP-B7 to calibrate)

| Bound | Threshold |
| --- | --- |
| Min throughput | 1,000 records/s |
| Max peak memory | 512 MB |
| Max aggregate ratio | 0.1 |
| Resume exactness | required |
| No host×vuln enumeration | required |

## For MSP-B7 (calibration input)

These are the first realistic VR scan-cycle volume signals for the MSP pack —
the VR analogue of B8's cloud-event month. Use them to confirm the shared
per-run budget covers scan-cycle bursts, not as final limits:

- **Per-record cost:** ~0.3154 ms/record to admit + fold, measured with tracemalloc active (a conservative ceiling).
- **A scan cycle of ~7,000 records** admits + folds in ~2.21 s here, well within a batch/offline window.
- **Memory is bounded by workflow patterns, not record volume** (1.43 MB peak for 60 patterns) — a scan re-finds the same estate, so folding compresses the burst.
- **A budget breach defers loudly** with a per-table breakdown and a safe checkpoint, and the deferred tail resumes exactly (no duplication, no skip).
- **The shared B7 per-run budget (250,000) is reused verbatim** — no independent SecOps limit — so VR bursts and cloud events answer to one calibrated ceiling.

## Methodology & reproduction

- **Deterministic corpus:** index-driven generators (no randomness) over a bounded
  estate, so the same counts reproduce the same corpus and comparable numbers.
- **Path:** burst records → `SecOpsVolumeStream.admit` (reusing `RunBudget` /
  `BudgetReport` from MSP-B7 and the T4 `remediation_signature` fold key) → workflow
  aggregates + budget/deferral report + safe checkpoint.
- **Reproduce** (writes this file):

  ```
  MSP_B11_VR_VOLUME_ITEMS=6000 \
  MSP_B11_VR_VOLUME_REPORT_OUT=docs/MSP-B11_VR_VOLUME_VALIDATION.md \
  python -m pytest discovery/tests/test_msp_b11_vr_volume.py
  ```
