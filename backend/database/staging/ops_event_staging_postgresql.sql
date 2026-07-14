-- =====================================================================
-- MSP-B8 Event-History Bridge — Staging Schema (PostgreSQL)
-- Schema contract version: 1.1.0
-- =====================================================================
--
-- The staging database contract for the Event-History Bridge. Load exported
-- AWS and Azure event history into this structure; AgentIQ's bridge ingestor
-- then reads it (read-only) and normalises each row through the MSP-B0 mappers.
--
-- This file is the STANDALONE partner artifact for a PostgreSQL staging store
-- (apply it with psql). It mirrors, statement for statement, the canonical DDL
-- in backend/database/models/ops_event_staging.py, which the alembic migration
-- 0026_create_ops_event_staging.py applies when AgentIQ's own PostgreSQL hosts
-- the store. A DB-free parity test
-- (backend/tests/unit/test_ops_event_staging_ddl_artifacts.py) fails if this
-- file and the model drift on columns or constraints — keep them in sync.
--
-- Column meanings, batch identification, org scoping, and the load contract are
-- documented in docs/MSP-B8_STAGING_SCHEMA.md (the partner-enablement guide).
--
-- Apply (connected to the target staging database):
--   psql -h <HOST> -p 5432 -U <USER> -d <DB> -v ON_ERROR_STOP=1 \
--        -f ops_event_staging_postgresql.sql
--
-- Idempotent: every statement is IF NOT EXISTS, so re-applying is a no-op.
-- =====================================================================

-- ---------------------------------------------------------------------
-- Operational event staging table.
--   row_id            store-owned monotonically increasing checkpoint key
--                     (GENERATED ALWAYS AS IDENTITY — a loader must not set it).
--   org_id            tenant scope; every bridge read is bound to one org.
--   provider          'aws' | 'azure' (open — not CHECK-constrained).
--   source_format     which standard export produced the row (routes the mapper).
--   batch_id          the export batch this row was loaded under.
--   provider_event_id the provider's own event identity — the idempotency key.
--   raw               the provider payload, kept intact (evidence resolution).
--   event_time        the provider event timestamp "where available" (v1.1.0);
--                     NULL when the record carries no parseable time. Staging
--                     metadata for bridge ordering/dedupe, not the detector-facing
--                     occurred_at (that is the B0 mapper's job).
--   loaded_at         when the row landed in staging (UTC).
-- The UNIQUE (org_id, provider, provider_event_id) constraint makes re-loading
-- an export batch produce zero duplicate rows (idempotent loads).
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ops_event_staging (
    row_id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    org_id            VARCHAR(64)              NOT NULL,
    provider          VARCHAR(32)              NOT NULL,
    source_format     VARCHAR(64)              NOT NULL,
    batch_id          VARCHAR(128)             NOT NULL,
    provider_event_id VARCHAR(256)             NOT NULL,
    raw               JSONB                    NOT NULL,
    loaded_at         TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    event_time        TIMESTAMP WITH TIME ZONE,
    CONSTRAINT uq_ops_event_staging_provider_event
        UNIQUE (org_id, provider, provider_event_id)
);

-- Org-scoped row-id paging — the bridge's incremental cursor.
CREATE INDEX IF NOT EXISTS idx_ops_event_staging_org_row
    ON ops_event_staging (org_id, row_id);

-- Batch lookup / re-load auditing.
CREATE INDEX IF NOT EXISTS idx_ops_event_staging_org_batch
    ON ops_event_staging (org_id, batch_id);

-- Provider/format-scoped reads. Provider-only filtering is already served by the
-- leading (org_id, provider) prefix of the unique constraint's index.
CREATE INDEX IF NOT EXISTS idx_ops_event_staging_org_format
    ON ops_event_staging (org_id, provider, source_format);

-- ---------------------------------------------------------------------
-- Companion batch registry — how export batches are identified and audited.
-- One row per load; loaders record record_count / skipped_count (loud-skips)
-- here. Intentionally NOT foreign-keyed to ops_event_staging (fail-open).
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ops_event_load_batches (
    org_id           VARCHAR(64)              NOT NULL,
    batch_id         VARCHAR(128)             NOT NULL,
    provider         VARCHAR(32)              NOT NULL,
    source_format    VARCHAR(64)              NOT NULL,
    source_reference TEXT,
    record_count     INTEGER                  NOT NULL DEFAULT 0,
    skipped_count    INTEGER                  NOT NULL DEFAULT 0,
    loaded_at        TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    PRIMARY KEY (org_id, batch_id)
);
