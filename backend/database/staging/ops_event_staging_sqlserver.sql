-- =====================================================================
-- MSP-B8 Event-History Bridge — Staging Schema (SQL Server)
-- Schema contract version: 1.0.0
-- =====================================================================
--
-- The staging database contract for the Event-History Bridge, SQL Server
-- dialect. Load exported AWS and Azure event history into this structure;
-- AgentIQ's bridge ingestor then reads it (read-only) over the native DB
-- connector and normalises each row through the MSP-B0 mappers.
--
-- This is the STANDALONE partner artifact for a SQL Server staging store. It is
-- the column-for-column, constraint-for-constraint SQL Server equivalent of the
-- PostgreSQL schema (backend/database/models/ops_event_staging.py and
-- ops_event_staging_postgresql.sql). A DB-free parity test
-- (backend/tests/unit/test_ops_event_staging_ddl_artifacts.py) fails if this
-- file drifts from that schema — keep the three in sync.
--
-- Dialect mapping vs PostgreSQL:
--   BIGINT GENERATED ALWAYS AS IDENTITY  ->  BIGINT IDENTITY(1,1)
--   VARCHAR(n)                           ->  NVARCHAR(n)
--   JSONB                                ->  NVARCHAR(MAX) + CHECK (ISJSON(raw) = 1)
--   TIMESTAMP WITH TIME ZONE / now()     ->  DATETIME2 (UTC) / SYSUTCDATETIME()
-- Both dialects store the same logical shape; row_id is a store-owned monotonic
-- identity in both.
--
-- Column meanings, batch identification, org scoping, and the load contract are
-- documented in docs/MSP-B8_STAGING_SCHEMA.md (the partner-enablement guide).
--
-- Apply (connected to the target staging database):
--   sqlcmd -S <HOST> -d <DB> -b -i ops_event_staging_sqlserver.sql
--
-- Idempotent: each object is guarded by an OBJECT_ID / index existence check.
-- =====================================================================

-- ---------------------------------------------------------------------
-- Operational event staging table.
--   row_id            store-owned monotonically increasing checkpoint key
--                     (IDENTITY(1,1) — a loader must not set it; if a bulk load
--                     needs to, wrap the insert in SET IDENTITY_INSERT ON/OFF).
--   org_id            tenant scope; every bridge read is bound to one org.
--   provider          'aws' | 'azure' (open — not CHECK-constrained).
--   source_format     which standard export produced the row (routes the mapper).
--   batch_id          the export batch this row was loaded under.
--   provider_event_id the provider's own event identity — the idempotency key.
--   raw               the provider payload, kept intact (evidence resolution).
--   loaded_at         when the row landed in staging (UTC).
-- The UNIQUE (org_id, provider, provider_event_id) constraint makes re-loading
-- an export batch produce zero duplicate rows (idempotent loads).
-- ---------------------------------------------------------------------
IF OBJECT_ID(N'dbo.ops_event_staging', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.ops_event_staging (
        row_id            BIGINT IDENTITY(1,1) NOT NULL
                              CONSTRAINT pk_ops_event_staging PRIMARY KEY,
        org_id            NVARCHAR(64)         NOT NULL,
        provider          NVARCHAR(32)         NOT NULL,
        source_format     NVARCHAR(64)         NOT NULL,
        batch_id          NVARCHAR(128)        NOT NULL,
        provider_event_id NVARCHAR(256)        NOT NULL,
        raw               NVARCHAR(MAX)        NOT NULL
                              CONSTRAINT ck_ops_event_staging_raw_json
                              CHECK (ISJSON(raw) = 1),
        loaded_at         DATETIME2            NOT NULL
                              CONSTRAINT df_ops_event_staging_loaded_at
                              DEFAULT SYSUTCDATETIME(),
        CONSTRAINT uq_ops_event_staging_provider_event
            UNIQUE (org_id, provider, provider_event_id)
    );
END;
GO

-- Org-scoped row-id paging — the bridge's incremental cursor.
IF NOT EXISTS (
    SELECT 1 FROM sys.indexes
    WHERE name = N'idx_ops_event_staging_org_row'
      AND object_id = OBJECT_ID(N'dbo.ops_event_staging')
)
    CREATE INDEX idx_ops_event_staging_org_row
        ON dbo.ops_event_staging (org_id, row_id);
GO

-- Batch lookup / re-load auditing.
IF NOT EXISTS (
    SELECT 1 FROM sys.indexes
    WHERE name = N'idx_ops_event_staging_org_batch'
      AND object_id = OBJECT_ID(N'dbo.ops_event_staging')
)
    CREATE INDEX idx_ops_event_staging_org_batch
        ON dbo.ops_event_staging (org_id, batch_id);
GO

-- Provider/format-scoped reads. Provider-only filtering is already served by the
-- leading (org_id, provider) prefix of the unique constraint's index.
IF NOT EXISTS (
    SELECT 1 FROM sys.indexes
    WHERE name = N'idx_ops_event_staging_org_format'
      AND object_id = OBJECT_ID(N'dbo.ops_event_staging')
)
    CREATE INDEX idx_ops_event_staging_org_format
        ON dbo.ops_event_staging (org_id, provider, source_format);
GO

-- ---------------------------------------------------------------------
-- Companion batch registry — how export batches are identified and audited.
-- One row per load; loaders record record_count / skipped_count (loud-skips)
-- here. Intentionally NOT foreign-keyed to ops_event_staging (fail-open).
-- ---------------------------------------------------------------------
IF OBJECT_ID(N'dbo.ops_event_load_batches', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.ops_event_load_batches (
        org_id           NVARCHAR(64)  NOT NULL,
        batch_id         NVARCHAR(128) NOT NULL,
        provider         NVARCHAR(32)  NOT NULL,
        source_format    NVARCHAR(64)  NOT NULL,
        source_reference NVARCHAR(MAX) NULL,
        record_count     INT           NOT NULL
                             CONSTRAINT df_ops_event_load_batches_rc DEFAULT 0,
        skipped_count    INT           NOT NULL
                             CONSTRAINT df_ops_event_load_batches_sc DEFAULT 0,
        loaded_at        DATETIME2     NOT NULL
                             CONSTRAINT df_ops_event_load_batches_loaded_at
                             DEFAULT SYSUTCDATETIME(),
        CONSTRAINT pk_ops_event_load_batches PRIMARY KEY (org_id, batch_id)
    );
END;
GO
