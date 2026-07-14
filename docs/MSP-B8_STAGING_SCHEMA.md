# MSP-B8 Event-History Bridge — Staging Schema (Partner Guide)

**Schema contract version:** `1.0.0`
**Audience:** partner / customer engineers performing export-and-load, with **no
CloudFulcrum assistance required** (this document is the partner-enablement
deliverable — MSP-B8 AC8).

## 1. What this is

The Event-History Bridge is AgentIQ's **export-and-load** path for cloud events.
Instead of AgentIQ calling AWS or Azure control planes directly, you **export**
event history from the provider, **load** it into a staging database using this
schema, and AgentIQ's *bridge ingestor* reads that staging database (read-only)
and normalises each row into the same events the native connectors produce.

```
  AWS / Azure export files ──(loaders)──▶  staging DB (this schema)  ──(bridge, read-only)──▶  AgentIQ
```

This is a supported product mode, not a demo hack: in boundaries where outbound
API access from a third-party platform is prohibited, **the bridge is the
deployment mode** — export inside the boundary, load inside the boundary, discover
inside the boundary.

Your responsibility is the middle box: **create the staging database and load
your exports into it.** That is what this document covers. The loaders (T2/T3) and
the bridge ingestor (T4) are separate deliverables; this schema is the contract
between them.

## 2. Supported staging stores

The staging store is a database **you provision**. Two engines are supported, with
identical logical shape:

| Engine | Apply with | DDL artifact |
| --- | --- | --- |
| PostgreSQL | `psql` | [`backend/database/staging/ops_event_staging_postgresql.sql`](../backend/database/staging/ops_event_staging_postgresql.sql) |
| SQL Server | `sqlcmd` | [`backend/database/staging/ops_event_staging_sqlserver.sql`](../backend/database/staging/ops_event_staging_sqlserver.sql) |

If AgentIQ's own PostgreSQL hosts the staging store, the schema is instead created
by the alembic migration `0026_create_ops_event_staging.py` — the DDL is identical
either way (both derive from the single source of truth in
`backend/database/models/ops_event_staging.py`).

## 3. Applying the DDL

Both scripts are **idempotent** — re-applying them is a no-op — and create no
data, only structure.

**PostgreSQL:**

```bash
psql -h <HOST> -p 5432 -U <USER> -d <STAGING_DB> -v ON_ERROR_STOP=1 \
     -f backend/database/staging/ops_event_staging_postgresql.sql
```

**SQL Server:**

```bash
sqlcmd -S <HOST> -d <STAGING_DB> -b \
       -i backend/database/staging/ops_event_staging_sqlserver.sql
```

The applying role needs `CREATE TABLE` / `CREATE INDEX` on the target database.
AgentIQ's bridge reads with a **read-only, fail-closed** account — see §7.

## 4. Tables

### 4.1 `ops_event_staging` — the operational event staging table

One row per exported event. Loaders insert into it; the bridge reads from it.

| Column | PostgreSQL | SQL Server | Null? | Meaning |
| --- | --- | --- | --- | --- |
| `row_id` | `BIGINT` identity | `BIGINT IDENTITY(1,1)` | no (PK) | **Monotonically increasing, store-assigned checkpoint key.** The bridge pages by it (§6). A loader must **not** set it — the database owns it. |
| `org_id` | `VARCHAR(64)` | `NVARCHAR(64)` | no | **Tenant scope.** Every bridge read is bound to one `org_id` (§7). You set this at load time. |
| `provider` | `VARCHAR(32)` | `NVARCHAR(32)` | no | `aws` or `azure`. Open column — a future provider needs no schema change. |
| `source_format` | `VARCHAR(64)` | `NVARCHAR(64)` | no | Which standard export produced the row (see §5). Selects the mapper. |
| `batch_id` | `VARCHAR(128)` | `NVARCHAR(128)` | no | The export batch this row was loaded under (§5.1). |
| `provider_event_id` | `VARCHAR(256)` | `NVARCHAR(256)` | no | **The idempotency key** — the provider's own event identity (see §5.2). |
| `raw` | `JSONB` | `NVARCHAR(MAX)` (valid JSON) | no | **The provider payload, intact.** Never transform it at load time — mapping and evidence resolution happen downstream against this exact record. |
| `loaded_at` | `TIMESTAMPTZ` (`now()`) | `DATETIME2` UTC (`SYSUTCDATETIME()`) | no | When the row landed in staging (UTC). Defaulted — leave it to the database. |

**Constraints & indexes:**

- `PRIMARY KEY (row_id)` — the checkpoint ordering.
- `UNIQUE (org_id, provider, provider_event_id)` — **duplicate prevention.**
  Re-loading the same export batch inserts nothing new (§5.2).
- `idx_ops_event_staging_org_row (org_id, row_id)` — org-scoped row-id paging.
- `idx_ops_event_staging_org_batch (org_id, batch_id)` — batch lookup / auditing.
- `idx_ops_event_staging_org_format (org_id, provider, source_format)` —
  format-scoped reads. Provider-only filtering is served by the leading
  `(org_id, provider)` prefix of the unique constraint's index.

### 4.2 `ops_event_load_batches` — the batch registry (companion)

One row per load operation, so a batch's provenance and counts are visible without
scanning the events table. Recommended but not required — it is **not**
foreign-keyed to `ops_event_staging` (a load is never blocked by registry state).

| Column | Meaning |
| --- | --- |
| `org_id`, `batch_id` | Primary key — identifies the batch (see §5.1). |
| `provider`, `source_format` | What was loaded. |
| `source_reference` | Free text — the file/dir/export the batch came from. |
| `record_count` | Rows inserted into `ops_event_staging` for this batch. |
| `skipped_count` | Malformed records loud-skipped during load (never silently dropped). |
| `loaded_at` | When the batch was loaded (UTC). |

## 5. The load contract

### 5.1 How export batches are identified

A **batch** is one load operation — typically one export file, one export folder,
or one export run. You choose the `batch_id`; make it:

- **Stable** for a given export — so re-loading the same export re-uses the same
  `batch_id` (aids auditing), and
- **Unique** across distinct exports for the same org.

A good convention is `<provider>:<source_format>:<export-identifier>`, e.g.
`aws:cloudtrail:2026-06` or `azure:azure_activity_log:2026-06-region-eastus`.
Record the batch in `ops_event_load_batches` with its `record_count` and
`skipped_count`.

### 5.2 Idempotency — `provider_event_id`

`provider_event_id` is the deduplication key. Populate it from the provider's own
event identity:

| `source_format` | Suggested `provider_event_id` source |
| --- | --- |
| `cloudtrail` | CloudTrail `eventID` |
| `cloudwatch_alarm_history` | alarm name + `HistoryItemType` + timestamp (composite) |
| `eventbridge_archive` | event `id` |
| `azure_monitor` | alert `id` / `alertId` |
| `azure_activity_log` | activity `eventDataId` / correlation+operation id |

When an export record has **no** natural stable id, compute a deterministic hash
of the raw record and use that — the same record then always yields the same id.

Because of the `UNIQUE (org_id, provider, provider_event_id)` constraint,
**re-loading an export batch produces zero duplicate rows** (MSP-B8 AC3). Loaders
insert with:

- PostgreSQL: `INSERT ... ON CONFLICT (org_id, provider, provider_event_id) DO NOTHING`
- SQL Server: an existence guard (or `MERGE ... WHEN NOT MATCHED`).

Duplicate rows never reach staging, so duplicate events never reach AgentIQ.

### 5.3 Preserve the raw payload

Store the provider record in `raw` **exactly as exported** (valid JSON). Downstream
mapping (MSP-B0) and evidence resolution read this field; anything you strip at
load time is lost to the evidence trace.

### 5.4 Malformed records

A record that cannot be parsed into a valid `raw` JSON payload with the required
identifying fields must be **loud-skipped with a reason and counted** in
`ops_event_load_batches.skipped_count` — never silently dropped, never allowed to
poison the rest of the batch (MSP-B8 AC5). This is the load-side responsibility of
the loaders (T2/T3); the schema supports it via the skip counter.

## 6. Incremental reads (row-id checkpointing)

The bridge is a change-based ingestor. It stores the highest `row_id` it has
processed for each `(org_id, connector)` and, on the next run, reads only newer
rows:

```sql
SELECT row_id, provider, source_format, provider_event_id, raw
FROM   ops_event_staging
WHERE  org_id = :org_id
  AND  row_id > :checkpoint
ORDER  BY row_id ASC
LIMIT  :batch;                 -- SQL Server: SELECT TOP (:batch) ... ORDER BY row_id
```

So a run after new rows are loaded processes **only** the new rows (MSP-B8 AC4).

**Operational assumption (important):** `row_id` is monotonic but not gap-free
(rolled-back loads leave gaps — harmless for a `>` cursor). To keep the cursor
correct under the identity model, **load each batch to completion before running
ingestion, and do not interleave a long-running load with an ingestion run.** V1
export-and-load is naturally batch-at-a-time, so this is the normal workflow, not
a restriction.

## 7. Org scoping

Multi-tenancy is enforced by `org_id`, exactly as elsewhere in AgentIQ:

- **At load time,** set `org_id` on every row to the AgentIQ org that owns the
  data. A single staging database can hold multiple orgs; rows never mix because
  every bridge query filters `WHERE org_id = ?`.
- **The default local org is `default`** — use it for single-tenant / demo loads
  unless you have been given a specific org id.
- **The bridge account is read-only and fail-closed:** grant AgentIQ's staging DB
  user `SELECT` on `ops_event_staging` (and, optionally, `ops_event_load_batches`)
  and nothing else. It never writes to the staging store.

## 8. Versioning

This schema is versioned (`STAGING_SCHEMA_VERSION` in
`backend/database/models/ops_event_staging.py`, and the header of each `.sql`
artifact). The current contract is **`1.0.0`**. Any change to the staging shape
bumps this version and is reflected in the model, both `.sql` artifacts, and this
document together — a DB-free parity test keeps them from drifting.

## 9. Checklist — "can a partner engineer do this unaided?"

1. Provision a PostgreSQL **or** SQL Server database for staging.
2. Apply the matching `.sql` artifact (§3). Verify `ops_event_staging` and
   `ops_event_load_batches` exist.
3. Export event history from AWS / Azure in a supported format (§5).
4. Load rows: set `org_id`, `provider`, `source_format`, `batch_id`,
   `provider_event_id`, and intact `raw`; leave `row_id` / `loaded_at` to the DB;
   dedupe on `provider_event_id`; count skips.
5. Record the batch in `ops_event_load_batches`.
6. Grant AgentIQ a **read-only** account and hand over the connection details.

No undocumented assumptions, no CloudFulcrum help required.
