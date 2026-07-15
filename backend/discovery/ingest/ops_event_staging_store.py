"""Write layer for the MSP-B8 staging schema — the loaders' sink (T2).

The export loaders (T2 AWS, T3 Azure) parse standard provider exports and hand
rows to a :class:`StagingSink`. Keeping the sink behind a small protocol means:

  * the loaders carry NO database concern — parsing, skip discipline, and dedupe
    logic are pure and unit-testable without a database (the offline discovery
    test rule), and
  * the bridge ingestor (T4) reads the same table these rows land in.

Two sinks ship here:

  * :class:`DbStagingSink` — the production sink. Writes to ``ops_event_staging``
    in PostgreSQL via ``app.db``, org-scoped, with idempotency enforced at the
    database by the ``UNIQUE (org_id, provider, provider_event_id)`` constraint
    (``INSERT ... ON CONFLICT DO NOTHING``). Re-loading an export batch therefore
    inserts zero duplicate rows (MSP-B8 AC3). It also upserts the companion
    ``ops_event_load_batches`` registry so record/skip counts are visible without
    scanning the events table.
  * :class:`InMemoryStagingSink` — a dependency-free sink that enforces the SAME
    unique key, for offline tests, demos, and dry runs.

Both return the number of rows NEWLY inserted, so a loader can report "N loaded,
M were already present" honestly.
"""
from __future__ import annotations

import json
import logging
from contextlib import closing
from typing import Any, Dict, List, Protocol, Sequence, Tuple

from database.models.ops_event_staging import (
    ALL_OPS_EVENT_STAGING_DDL,
    OpsEventLoadBatch,
    OpsEventStagingRow,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Sink protocol
# ---------------------------------------------------------------------------


class StagingSink(Protocol):
    """Where a loader puts parsed staging rows and its batch summary."""

    def insert_rows(self, rows: Sequence[OpsEventStagingRow]) -> int:
        """Insert rows idempotently; return how many were NEWLY inserted.

        A row whose ``(org_id, provider, provider_event_id)`` already exists is a
        no-op — that is what makes a re-load produce zero duplicates (AC3).
        """
        ...

    def record_batch(self, batch: OpsEventLoadBatch) -> None:
        """Record (or refresh) the load-batch registry entry for this load."""
        ...


# ---------------------------------------------------------------------------
# Schema bootstrap (local/dev/test convenience; the migration is authoritative)
# ---------------------------------------------------------------------------


def ensure_ops_event_staging() -> None:
    """Create the staging tables/indexes if absent (idempotent).

    Runs the identical ``ALL_OPS_EVENT_STAGING_DDL`` that migration
    ``0027_create_ops_event_staging.py`` applies, so a runtime-created schema and a
    migration-applied schema can never drift — the same pattern as
    ``app.retrieval.store.ensure_retrieval_store``. Every statement is
    ``IF NOT EXISTS``.
    """
    from app import db

    with closing(db.connect()) as con:
        cur = con.cursor()
        for ddl in ALL_OPS_EVENT_STAGING_DDL:
            cur.execute(ddl)
        con.commit()


# ---------------------------------------------------------------------------
# Production sink — PostgreSQL
# ---------------------------------------------------------------------------

_INSERT_COLUMNS = (
    "org_id",
    "provider",
    "source_format",
    "batch_id",
    "provider_event_id",
    "raw",
    "event_time",
)


class DbStagingSink:
    """Writes staging rows to ``ops_event_staging`` in PostgreSQL (org-scoped).

    Idempotency lives at the database: the unique constraint plus
    ``ON CONFLICT DO NOTHING`` means a duplicated provider event — whether within
    one export or across re-loads of the same batch — is silently ignored, never
    duplicated (AC3). ``insert_rows`` returns the count actually inserted via
    ``RETURNING``.
    """

    def insert_rows(self, rows: Sequence[OpsEventStagingRow]) -> int:
        if not rows:
            return 0
        from app import db
        from psycopg2.extras import execute_values

        values = [
            (
                r.org_id,
                r.provider,
                r.source_format,
                r.batch_id,
                r.provider_event_id,
                json.dumps(r.raw),
                r.event_time,
            )
            for r in rows
        ]
        with closing(db.connect()) as con:
            cur = con.cursor()
            inserted = execute_values(
                cur,
                "INSERT INTO ops_event_staging "
                "(org_id, provider, source_format, batch_id, provider_event_id, "
                "raw, event_time) "
                "VALUES %s "
                "ON CONFLICT (org_id, provider, provider_event_id) DO NOTHING "
                "RETURNING 1",
                values,
                template="(%s, %s, %s, %s, %s, %s::jsonb, %s)",
                fetch=True,
            )
            con.commit()
        return len(inserted)

    def record_batch(self, batch: OpsEventLoadBatch) -> None:
        from app import db

        with closing(db.connect()) as con:
            cur = con.cursor()
            cur.execute(
                """
                INSERT INTO ops_event_load_batches
                    (org_id, batch_id, provider, source_format,
                     source_reference, record_count, skipped_count, loaded_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (org_id, batch_id) DO UPDATE SET
                    provider         = EXCLUDED.provider,
                    source_format    = EXCLUDED.source_format,
                    source_reference = EXCLUDED.source_reference,
                    record_count     = EXCLUDED.record_count,
                    skipped_count    = EXCLUDED.skipped_count,
                    loaded_at        = EXCLUDED.loaded_at
                """,
                (
                    batch.org_id,
                    batch.batch_id,
                    batch.provider,
                    batch.source_format,
                    batch.source_reference,
                    batch.record_count,
                    batch.skipped_count,
                    batch.loaded_at,
                ),
            )
            con.commit()


# ---------------------------------------------------------------------------
# In-memory sink — offline tests / demos / dry runs
# ---------------------------------------------------------------------------


class InMemoryStagingSink:
    """A dependency-free sink enforcing the SAME unique key as the database.

    Mirrors ``UNIQUE (org_id, provider, provider_event_id)`` so idempotency can be
    proven without a database: a repeated key is ignored, and ``insert_rows``
    returns only the newly inserted count. Rows and batches are kept in memory for
    assertions.
    """

    def __init__(self) -> None:
        self.rows: List[OpsEventStagingRow] = []
        self.batches: Dict[Tuple[str, str], OpsEventLoadBatch] = {}
        self._keys: set[Tuple[str, str, str]] = set()
        self._row_seq = 0

    def insert_rows(self, rows: Sequence[OpsEventStagingRow]) -> int:
        inserted = 0
        for r in rows:
            key = (r.org_id, r.provider, r.provider_event_id)
            if key in self._keys:
                continue
            self._keys.add(key)
            # Mint a store-owned monotonic row_id exactly as the DB IDENTITY does,
            # so this sink can also serve as a faithful StagingReader for the
            # bridge's row-id paging (T4) in DB-free tests.
            self._row_seq += 1
            if r.row_id is None:
                r.row_id = self._row_seq
            self.rows.append(r)
            inserted += 1
        return inserted

    def record_batch(self, batch: OpsEventLoadBatch) -> None:
        self.batches[(batch.org_id, batch.batch_id)] = batch

    # --- StagingReader surface (so tests write via the sink, read via it) --

    def fetch_after(
        self, org_id: str, *, after_row_id: int, limit: int
    ) -> List[OpsEventStagingRow]:
        """Return this org's rows with ``row_id > after_row_id`` (row-id paged).

        Mirrors :class:`DbStagingReader.fetch_after` — org-scoped, ordered by
        ``row_id``, capped at ``limit`` — so the bridge's incremental cursor logic
        is exercised identically with or without a database.
        """
        ordered = sorted(
            (r for r in self.rows if r.org_id == org_id and (r.row_id or 0) > after_row_id),
            key=lambda r: r.row_id or 0,
        )
        return ordered[:limit]

    # --- convenience for tests -------------------------------------------

    def rows_for(self, org_id: str) -> List[OpsEventStagingRow]:
        return [r for r in self.rows if r.org_id == org_id]


# ---------------------------------------------------------------------------
# Read side — the bridge's staging reader (read-only, fail-closed)
# ---------------------------------------------------------------------------


class StagingReader(Protocol):
    """Where the bridge ingestor (T4) reads staged rows from, row-id paged."""

    def fetch_after(
        self, org_id: str, *, after_row_id: int, limit: int
    ) -> Sequence[OpsEventStagingRow]:
        """Return up to ``limit`` rows for ``org_id`` with ``row_id > after_row_id``."""
        ...


class DbStagingReader:
    """Reads staged rows from ``ops_event_staging`` on the read-only DB path (T4).

    The bridge is a CONSUMER of the staging store, never a writer into it, so this
    reader is deliberately read-only and fail-closed:

      * **Read-only** — every fetch opens the transaction with
        ``SET TRANSACTION READ ONLY`` before the ``SELECT``, so any accidental
        write in the same transaction is rejected by PostgreSQL. The bridge cannot
        become a privileged write path into a partner database (MSP-B8 AC6). The
        read-only mode is per-transaction and clears on the ``closing`` rollback,
        so no pooled-connection state leaks to the next borrower.
      * **Fail-closed** — any DB error propagates; the caller (the ingestor) does
        not swallow it, so the checkpoint is never advanced over rows that were not
        actually read (MSP-B8 AC4 / AC6).

    Org-scoped and row-id paged via the ``(org_id, row_id)`` index (T1).
    """

    _SELECT = (
        "SELECT row_id, org_id, provider, source_format, batch_id, "
        "       provider_event_id, raw, event_time, loaded_at "
        "FROM ops_event_staging "
        "WHERE org_id = %s AND row_id > %s "
        "ORDER BY row_id ASC "
        "LIMIT %s"
    )

    def fetch_after(
        self, org_id: str, *, after_row_id: int, limit: int
    ) -> List[OpsEventStagingRow]:
        if not org_id:
            raise ValueError("org_id is required")
        from app import db

        with closing(db.connect()) as con:
            cur = con.cursor()
            cur.execute("SET TRANSACTION READ ONLY")
            cur.execute(self._SELECT, (org_id, int(after_row_id), int(limit)))
            fetched = cur.fetchall()
        return [_row_from_db(r) for r in fetched]


def _row_from_db(r: Any) -> OpsEventStagingRow:
    """Build an :class:`OpsEventStagingRow` from a DB row (dict-like or tuple)."""
    if hasattr(r, "keys"):  # DictCursor row
        raw = r["raw"]
        return OpsEventStagingRow(
            org_id=r["org_id"],
            provider=r["provider"],
            source_format=r["source_format"],
            batch_id=r["batch_id"],
            provider_event_id=r["provider_event_id"],
            raw=raw if isinstance(raw, dict) else json.loads(raw),
            row_id=r["row_id"],
            event_time=r["event_time"],
            loaded_at=r["loaded_at"],
        )
    row_id, org_id, provider, source_format, batch_id, peid, raw, event_time, loaded_at = r
    return OpsEventStagingRow(
        org_id=org_id,
        provider=provider,
        source_format=source_format,
        batch_id=batch_id,
        provider_event_id=peid,
        raw=raw if isinstance(raw, dict) else json.loads(raw),
        row_id=row_id,
        event_time=event_time,
        loaded_at=loaded_at,
    )
