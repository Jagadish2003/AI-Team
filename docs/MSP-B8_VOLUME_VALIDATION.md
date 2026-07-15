# MSP-B8 — Month-Scale Volume Validation (T5)

> Recorded evidence for MSP-B7 event-volume budget & operational-envelope
> calibration (AC7). Measured numbers, not assumptions.

- **Envelope result:** PASS
- **Environment:** Windows 11, Python 3.11.9, local PostgreSQL 16, single process, tracemalloc active (timings are conservative)
- **Org:** `org_volume_ac7`  |  **Bridge batch size:** 500

## Corpus (representative month of AWS + Azure exports)

| Surface | Records |
| --- | --- |
| `eventbridge_archive` | 7,575 |
| `cloudtrail` | 7,575 |
| `azure_monitor` | 7,500 |
| `azure_activity_log` | 7,575 |
| **Total generated** | **30,225** |
| _of which malformed (injected)_ | 447 |
| _of which duplicates (injected)_ | 225 |

## Load (T2/T3 loaders → staging)

| Metric | Value |
| --- | --- |
| Load time | 29.888 s |
| Rows loaded to staging | 29,553 |
| Rows loud-skipped (malformed) | 447 |
| Duplicates collapsed | 225 |
| **Load throughput** | **1,011.3 rows/s** |

## Ingest (T4 bridge → normalized events)

| Metric | Value |
| --- | --- |
| Ingest time | 43.559 s |
| Normalized events emitted | 29,553 |
| Bridge batches | 60 |
| Max batch size | 500 |
| Final row-id checkpoint | 29553 |
| **Ingest throughput** | **678.5 events/s** |

## Correctness at volume

| Metric | Value |
| --- | --- |
| Events with recurrence signature | 29,553 / 29,553 |
| Evidence traces resolved (sample) | 25 / 25 |
| Resume: new rows processed | 200 (of 200 added) |

## Resource pressure

| Metric | Value |
| --- | --- |
| Peak memory (tracemalloc) | 89.61 MB |

## Envelope (proposed — MSP-B7 to calibrate)

| Bound | Threshold |
| --- | --- |
| Min load throughput | 500 rows/s |
| Min ingest throughput | 500 events/s |
| Max peak memory | 1,024 MB |
| Resume exactness | required |
| Evidence resolves | required |

## For MSP-B7 (calibration input) — CONSUMED by T6 (AT-674)

> These numbers are now the calibration input for the MSP-B7 volume defaults.
> `backend/discovery/signals/ops_calibration.py` captures them verbatim
> (`B8_MEASUREMENTS`) and derives: per-run event budget = **250,000** (ceil(8 ×
> 30,225 month) → rounded), noise floors (audit/state_change/access = 5; else 1),
> and correlation windows (event↔event 15 min, event↔incident 2 h). See
> `docs/msp_operational_event_schema.md` §13 for the full derivation and rationale.

These are the first realistic event-volume signals for the MSP pack — use
them to seed the budget/operational-envelope, not as final limits:

- **Per-event cost:** ~1.011 ms/event to load and ~1.474 ms/event to ingest through the bridge, measured with tracemalloc active (a conservative ceiling; real throughput is higher).
- **A month of ~30,225 events** loads + ingests end to end in ~73 s here, well within a batch/offline window.
- **Memory is flat and small** (89.61 MB peak) — the loaders and the bridge stream in bounded batches, so volume drives time, not memory.
- **Skip + dedupe accounting is exact and visible**, so a partner can trust the loaded-vs-exported reconciliation at scale.
- **Ingestion is incremental** — a resume after new rows processed only the new rows, so steady-state runs cost per-delta, not per-history.

## Methodology & reproduction

- **Deterministic corpus:** index-driven generators (no randomness), so the same
  counts reproduce the same corpus and comparable numbers. A fixed slice is injected
  malformed (missing id → loud-skip) and duplicated (→ dedupe) to exercise accounting.
- **Full path:** T2/T3 loaders → `ops_event_staging` → T4 bridge (read-only DB path,
  row-id checkpoint paging) → normalized `OperationalEvent`s with resolvable evidence.
- **Reproduce** (writes this file), against the disposable test DB:

  ```
  MSP_B8_VOLUME_PER_FORMAT=7500 \
  MSP_B8_VOLUME_REPORT_OUT=docs/MSP-B8_VOLUME_VALIDATION.md \
  python -m pytest backend/tests/contract/test_ops_event_volume.py
  ```

- **Scope — CloudWatch:** the mix uses the four surfaces whose export shape and B0
  mapper align cleanly today (EventBridge, CloudTrail, Azure Monitor, Azure Activity
  Log). CloudWatch alarm-history ↔ `map_cloudwatch` shape reconciliation is tracked
  with the T6 equivalence work and is intentionally excluded so a degraded surface
  does not skew these numbers.
