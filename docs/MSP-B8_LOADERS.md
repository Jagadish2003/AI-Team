# MSP-B8 Event-History Bridge — Export Loaders (Partner Guide)

> Companion to [`MSP-B8_STAGING_SCHEMA.md`](MSP-B8_STAGING_SCHEMA.md). That guide
> defines the staging **store**; this one documents how to get your exported cloud
> events **into** it. Together they let a partner engineer perform export-and-load
> with no CloudFulcrum assistance (MSP-B8 AC8).

## 1. Where loaders sit

```
AWS / Azure export files ──(loaders, this doc)──▶ ops_event_staging ──(bridge, read-only)──▶ AgentIQ
```

A **loader** parses one standard provider export format and writes rows into the
`ops_event_staging` table. It does **not** normalise, map, or score — it preserves
the raw payload verbatim and records just enough metadata for the bridge to later
map each row through the MSP-B0 mappers. Loaders are pure over an injectable
sink, so they can run against the real database or in memory (tests/dry runs).

The loaders live in:

- `backend/discovery/ingest/aws_export_loaders.py`
- `backend/discovery/ingest/azure_export_loaders.py`

## 2. The loaders

| Loader | Provider | `source_format` | Export shape |
| --- | --- | --- | --- |
| `load_cloudwatch_alarm_history` | `aws` | `cloudwatch_alarm_history` | CloudWatch alarm history (`DescribeAlarmHistory` output) |
| `load_eventbridge_archive` | `aws` | `eventbridge_archive` | EventBridge archive export (event envelopes) |
| `load_cloudtrail_logs` | `aws` | `cloudtrail` | CloudTrail log files (`.json` / `.json.gz`) |
| `load_azure_monitor_alerts` | `azure` | `azure_monitor` | Azure Monitor alert export (common alert schema) |
| `load_azure_activity_log` | `azure` | `azure_activity_log` | Azure Activity Log export |

Scope (V1): the providers' **standard** export/JSON shapes only — bespoke SIEM
re-exports and tenant-specific transforms are out of scope, matching the B1/B2
native-connector event classes (alarms/alerts, state changes, audit).

## 3. Calling a loader

Every loader has the same signature:

```python
from discovery.ingest.aws_export_loaders import load_cloudtrail_logs
from discovery.ingest.ops_event_staging_store import DbStagingSink

result = load_cloudtrail_logs(
    source,                 # a file path, a directory, or an in-memory list/dict
    org_id="acme",          # REQUIRED — every row is org-scoped
    batch_id=None,          # optional; a deterministic default is derived if omitted
    sink=DbStagingSink(),   # where rows are written (defaults to the DB sink)
)
```

**`source` accepts:**

- a **file** path — a single export file (`.json`, `.jsonl`; CloudTrail also `.json.gz`);
- a **directory** — every matching file in it is loaded (one bad file never
  poisons the others);
- an **in-memory** `list` (records) or `dict` (a provider container such as
  `{"Records": [...]}` / `{"AlarmHistoryItems": [...]}` / `{"value": [...]}`).

Each format also accepts JSON-lines (one JSON object per line); a single bad line
is loud-skipped, the rest load.

**`sink`:** use `DbStagingSink()` for the real database, or
`InMemoryStagingSink()` for a dry run / test. Call
`ops_event_staging_store.ensure_ops_event_staging()` once to create the tables in
dev (in production apply the DDL / migration — see the schema guide §3).

## 4. `provider_event_id` — the idempotency key

Each loader extracts the provider's own event identity into `provider_event_id`,
which the unique constraint `(org_id, provider, provider_event_id)` dedupes on:

| `source_format` | `provider_event_id` |
| --- | --- |
| `eventbridge_archive` | envelope `id` |
| `cloudtrail` | record `eventID` |
| `azure_monitor` | alert `id` / `alertId` |
| `azure_activity_log` | `eventDataId` (else `id`) |
| `cloudwatch_alarm_history` | composite: alarm name + `HistoryItemType` + timestamp |

A record with no extractable identity is **loud-skipped** (`missing_event_id`) — it
is never loaded without a dedupe key.

## 5. Batch id

Pass `batch_id` to label a load (recommended: `"<provider>:<source_format>:<window>"`,
e.g. `"aws:cloudtrail:2026-06"`). If omitted, a **deterministic** default is
derived from the source name (`aws:cloudtrail:<file>`), so re-loading the same
source reuses the same batch id — handy for auditing. Idempotency holds via
`provider_event_id` regardless of the batch id. Each load upserts a row into the
`ops_event_load_batches` registry with its `record_count` and `skipped_count`.

## 6. Malformed records — loud-skip, never silent

Malformed or unusable records are **skipped with a reason, counted, and logged at
WARNING**, while every valid record in the same batch still loads (the R18-A1
discipline). Skip reasons:

| Reason | Meaning |
| --- | --- |
| `malformed_json` | a record / file / JSONL line is not valid JSON |
| `not_an_object` | a parsed record is not a JSON object |
| `missing_event_id` | no extractable `provider_event_id` (cannot dedupe it) |
| `unreadable_file` | a file could not be read |

## 7. What a load returns (`LoadResult`)

```python
result.record_count      # valid records parsed (the dedupe input)
result.inserted_count    # rows NEWLY written to staging this run
result.duplicate_count   # valid records already present (within-batch + re-loads)
result.skipped_count     # malformed records loud-skipped
result.skipped           # list of SkippedRecord(reason, detail, source_reference, index)
result.batch_id          # the batch id used
```

Re-running a load inserts **zero** new rows for events already staged
(`inserted_count == 0`, `duplicate_count` rises) — safe to re-run.

## 8. Worked examples

```python
from discovery.ingest.aws_export_loaders import (
    load_eventbridge_archive, load_cloudtrail_logs,
)
from discovery.ingest.azure_export_loaders import (
    load_azure_monitor_alerts, load_azure_activity_log,
)
from discovery.ingest.ops_event_staging_store import DbStagingSink

sink = DbStagingSink()

# A directory of gzipped CloudTrail log files:
load_cloudtrail_logs("/exports/cloudtrail/2026-06/", org_id="acme",
                     batch_id="aws:cloudtrail:2026-06", sink=sink)

# An EventBridge archive export file:
load_eventbridge_archive("/exports/eventbridge/june.json", org_id="acme", sink=sink)

# Azure Monitor alerts + Activity Log:
load_azure_monitor_alerts("/exports/azure/monitor.json", org_id="acme", sink=sink)
load_azure_activity_log("/exports/azure/activity.json", org_id="acme", sink=sink)
```

Once loaded, the bridge (`OpsEventBridgeIngestor`) reads the staged rows on the
read-only DB path and normalises them — you do not call it directly; it runs as a
change-based ingestor. See the schema guide §6 for the row-id checkpoint model.

## 9. Checklist — "can a partner engineer load unaided?"

1. Provision the staging store and apply the DDL (schema guide §3). ✅
2. Pick the loader matching your export format (§2). ✅
3. Call it with your `org_id`, a `batch_id`, and a `DbStagingSink` (§3). ✅
4. Confirm `LoadResult`: `inserted_count` matches expectations; review any
   `skipped` records and their reasons (§6, §7). ✅
5. Re-run safely if needed — it is idempotent on `provider_event_id` (§4, §7). ✅
