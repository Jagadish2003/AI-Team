"""Staging-schema model for the Event-History Bridge — MSP-B8 / T1.

The **staging database contract**: the known structure a partner engineer loads
exported AWS and Azure event history into, before AgentIQ's bridge ingestor
(MSP-B8 / T4) reads it on the existing read-only, fail-closed DB path and maps
each raw payload through the MSP-B0 mappers.

This module is the SINGLE SOURCE OF TRUTH for the PostgreSQL staging schema:

  * ``migrations/versions/0027_create_ops_event_staging.py`` (the CI gate)
    imports ``ALL_OPS_EVENT_STAGING_DDL`` from here, and
  * the partner-facing PostgreSQL artifact
    ``database/staging/ops_event_staging_postgresql.sql`` mirrors it verbatim
    (a DB-free parity test — ``tests/unit/test_ops_event_staging_ddl_artifacts.py``
    — fails if the two drift on columns/constraints).

so the migration-applied schema, the shipped artifact, and the SQL Server artifact
can never silently diverge — the same no-drift discipline used by
``database/models/entities.py`` and ``database/models/retrieval.py``.

The staging store may be hosted in AgentIQ's own PostgreSQL (created by the
migration below) OR in a partner-provisioned PostgreSQL / SQL Server the bridge
reads over the native DB connector. Either way the *shape* is identical; only the
applier differs. See ``docs/MSP-B8_STAGING_SCHEMA.md`` for the partner-enablement
contract (AC8).

Schema shape (Section 1 of the story fixes the required fields):

  * ``row_id`` — a monotonically increasing, server-assigned identity. This is the
    checkpoint key: the bridge is a ``ChangeBasedIngestor`` that pages
    ``WHERE org_id = ? AND row_id > <checkpoint> ORDER BY row_id`` and never
    rereads the full export (AC4). Declared ``GENERATED ALWAYS AS IDENTITY`` so a
    loader can never set it — the store owns the ordering. Gaps (from rolled-back
    loads) are expected and harmless: the cursor is ``>``, not a counter.
  * ``org_id`` — mandatory tenant scope. Every bridge read binds it, so one
    partner's staged events are never ingested into another org (AC6), consistent
    with R17-D3 tenant isolation.
  * ``provider`` — ``'aws'`` / ``'azure'`` (open, see below).
  * ``source_format`` — which standard export a row came from
    (``cloudwatch_alarm_history`` / ``eventbridge_archive`` / ``cloudtrail`` /
    ``azure_monitor`` / ``azure_activity_log``). The bridge routes
    ``(provider, source_format)`` to the right MSP-B0 mapper, and it makes
    provider/format filtering index-served.
  * ``batch_id`` — the export batch a row was loaded under (see
    ``ops_event_load_batches``). Batch lookup + re-load auditing.
  * ``provider_event_id`` — the provider's own event identity, the idempotency key.
    A UNIQUE ``(org_id, provider, provider_event_id)`` constraint dedupes at the
    door, so re-loading the same export batch produces zero duplicate rows and
    therefore zero duplicate events (AC3). Loaders insert with
    ``ON CONFLICT DO NOTHING`` (PostgreSQL) / an existence guard (SQL Server).
  * ``raw`` — the provider payload kept fully intact (``JSONB``), so evidence
    resolution can point back at the exact source record. Never lossily
    transformed at load time — mapping happens later, in the bridge.
  * ``event_time`` — the provider event timestamp, extracted "where available"
    by the loaders (T3, v1.1.0). Nullable staging metadata for bridge ordering /
    dedupe; NOT the detector-facing ``occurred_at`` (that is normalised by the B0
    mapper in T4). Left NULL when the source record carries no parseable time.
  * ``loaded_at`` — when the row landed in staging (UTC), for load auditing and
    the MSP-B7 volume signal.

``provider`` is deliberately NOT constrained by a CHECK: V1 ships AWS + Azure, but
a future provider must be able to load without a schema migration — the same
open-column stance ``retrieval.py`` takes for ``source_system``. Expected values
are documented, not enforced in DDL.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

# Versioned contract. Bump on any change to the staging shape and record it in
# both partner .sql headers and docs/MSP-B8_STAGING_SCHEMA.md. Partner engineers
# key their load tooling to this version.
#   1.0.0 — initial staging schema (T1).
#   1.1.0 — add nullable event_time column (T3): the provider event timestamp,
#           extracted "where available" by the loaders (additive, backward
#           compatible — existing rows/loaders leave it NULL).
STAGING_SCHEMA_VERSION = "1.1.0"

# Providers V1 loads. NOT a DDL CHECK — kept here so loaders/tests/docs agree on
# the expected set while the column stays open to future providers.
KNOWN_PROVIDERS = frozenset({"aws", "azure"})

# Standard export formats V1 recognises. Mirrors the B1/B2 V1 event-class scope
# (alarms/alerts, state changes, audit) — the bridge never claims wider coverage
# than the native connectors will. Also open (no CHECK) for the same reason.
KNOWN_SOURCE_FORMATS = frozenset(
    {
        "cloudwatch_alarm_history",  # AWS — alarm state changes
        "eventbridge_archive",       # AWS — archived events
        "cloudtrail",                # AWS — audit log files
        "azure_monitor",             # Azure — Monitor alert export
        "azure_activity_log",        # Azure — Activity Log export
    }
)

# Column widths — bounded so the (org_id, provider, provider_event_id) unique
# index stays inside PostgreSQL's btree row limit. provider_event_id is the
# widest because CloudTrail eventIDs / Azure activity operation ids are long, and
# a loader may fall back to a content hash when a native id is absent.
_ORG_ID_LEN = 64
_PROVIDER_LEN = 32
_SOURCE_FORMAT_LEN = 64
_BATCH_ID_LEN = 128
_PROVIDER_EVENT_ID_LEN = 256

# ---------------------------------------------------------------------------
# DDL — single source of truth (imported by the migration; mirrored by the
# partner PostgreSQL .sql artifact, which a parity test pins to this).
# ---------------------------------------------------------------------------

# The operational event staging table. ``row_id`` is a store-owned monotonic
# identity (the checkpoint key). ``raw`` is JSONB so the payload is queryable yet
# preserved intact for evidence resolution.
CREATE_OPS_EVENT_STAGING_TABLE = f"""
    CREATE TABLE IF NOT EXISTS ops_event_staging (
        row_id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        org_id            VARCHAR({_ORG_ID_LEN})             NOT NULL,
        provider          VARCHAR({_PROVIDER_LEN})           NOT NULL,
        source_format     VARCHAR({_SOURCE_FORMAT_LEN})      NOT NULL,
        batch_id          VARCHAR({_BATCH_ID_LEN})           NOT NULL,
        provider_event_id VARCHAR({_PROVIDER_EVENT_ID_LEN})  NOT NULL,
        raw               JSONB                              NOT NULL,
        loaded_at         TIMESTAMP WITH TIME ZONE           NOT NULL DEFAULT now(),
        CONSTRAINT uq_ops_event_staging_provider_event
            UNIQUE (org_id, provider, provider_event_id)
    )
"""

# Org-scoped row-id paging — the bridge's incremental cursor
# (WHERE org_id = ? AND row_id > ? ORDER BY row_id). Leads with org_id so tenant
# scoping and paging are one index seek (AC4 + AC6).
CREATE_OPS_EVENT_STAGING_IDX_ORG_ROW = """
    CREATE INDEX IF NOT EXISTS idx_ops_event_staging_org_row
        ON ops_event_staging (org_id, row_id)
"""

# Batch lookup / re-load auditing — "show me everything loaded under batch X".
CREATE_OPS_EVENT_STAGING_IDX_ORG_BATCH = """
    CREATE INDEX IF NOT EXISTS idx_ops_event_staging_org_batch
        ON ops_event_staging (org_id, batch_id)
"""

# Provider / format filtering is served by the leading (org_id, provider) prefix
# of the UNIQUE constraint's index, so no separate provider index is needed. The
# (org_id, provider, source_format) index below narrows format-specific reads.
CREATE_OPS_EVENT_STAGING_IDX_ORG_FORMAT = """
    CREATE INDEX IF NOT EXISTS idx_ops_event_staging_org_format
        ON ops_event_staging (org_id, provider, source_format)
"""

# Companion batch registry — how export batches are identified and audited. One
# row per load; the loaders record record/skip counts here so a malformed-record
# loud-skip (AC5) and the month-scale volume signal (AC7) are visible without
# scanning the events table. Intentionally NOT foreign-keyed to the events table:
# a partner can use the events table alone, and a load failure must never be
# blocked by registry state (fail-open).
CREATE_OPS_EVENT_LOAD_BATCHES_TABLE = f"""
    CREATE TABLE IF NOT EXISTS ops_event_load_batches (
        org_id           VARCHAR({_ORG_ID_LEN})        NOT NULL,
        batch_id         VARCHAR({_BATCH_ID_LEN})      NOT NULL,
        provider         VARCHAR({_PROVIDER_LEN})      NOT NULL,
        source_format    VARCHAR({_SOURCE_FORMAT_LEN}) NOT NULL,
        source_reference TEXT,
        record_count     INTEGER                       NOT NULL DEFAULT 0,
        skipped_count    INTEGER                       NOT NULL DEFAULT 0,
        loaded_at        TIMESTAMP WITH TIME ZONE      NOT NULL DEFAULT now(),
        PRIMARY KEY (org_id, batch_id)
    )
"""

# T3 (v1.1.0) — the provider event timestamp, "where available". Nullable and
# added by ALTER (not folded into the CREATE above) so the migration path
# (0027 create → 0028 alter) and a fresh create both converge on the SAME column
# order — event_time last — and existing 0026 stores gain it without a rebuild.
# Same split-DDL pattern as ingestion_checkpoints (0017 create / 0018 alter).
# Not a detector-facing field: it is staging metadata for bridge ordering/dedupe;
# final occurred_at normalisation belongs to the B0 mapper (T4).
ALTER_OPS_EVENT_STAGING_ADD_EVENT_TIME = """
    ALTER TABLE ops_event_staging
        ADD COLUMN IF NOT EXISTS event_time TIMESTAMP WITH TIME ZONE
"""

ALL_OPS_EVENT_STAGING_DDL: tuple[str, ...] = (
    CREATE_OPS_EVENT_STAGING_TABLE,
    CREATE_OPS_EVENT_STAGING_IDX_ORG_ROW,
    CREATE_OPS_EVENT_STAGING_IDX_ORG_BATCH,
    CREATE_OPS_EVENT_STAGING_IDX_ORG_FORMAT,
    CREATE_OPS_EVENT_LOAD_BATCHES_TABLE,
    ALTER_OPS_EVENT_STAGING_ADD_EVENT_TIME,
)

# DROP order for downgrade() — indexes, then tables.
DROP_OPS_EVENT_STAGING_DDL: tuple[str, ...] = (
    "DROP INDEX IF EXISTS idx_ops_event_staging_org_format",
    "DROP INDEX IF EXISTS idx_ops_event_staging_org_batch",
    "DROP INDEX IF EXISTS idx_ops_event_staging_org_row",
    "DROP TABLE IF EXISTS ops_event_load_batches",
    "DROP TABLE IF EXISTS ops_event_staging",
)


@dataclass
class OpsEventStagingRow:
    """One staged operational event — the load-side record (MSP-B8 / T1).

    ``row_id`` and ``loaded_at`` are store-assigned and left ``None`` on the
    load-side record: the loader inserts every other field and the database mints
    the identity and load timestamp. The bridge ingestor (T4) reads rows back with
    ``row_id``/``loaded_at`` populated and maps ``raw`` through the B0 mappers.
    """

    org_id: str
    provider: str
    source_format: str
    batch_id: str
    provider_event_id: str
    raw: dict[str, Any]
    event_time: Optional[datetime] = None
    row_id: Optional[int] = None
    loaded_at: Optional[datetime] = None

    def __post_init__(self) -> None:
        for fname in (
            "org_id",
            "provider",
            "source_format",
            "batch_id",
            "provider_event_id",
        ):
            val = getattr(self, fname)
            if val is None or not str(val).strip():
                raise ValueError(f"{fname} is required")
        if self.raw is None:
            raise ValueError("raw payload is required and must be preserved intact")


@dataclass
class OpsEventLoadBatch:
    """A load-batch registry entry — how an export batch is identified (T1)."""

    org_id: str
    batch_id: str
    provider: str
    source_format: str
    source_reference: Optional[str] = None
    record_count: int = 0
    skipped_count: int = 0
    loaded_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
