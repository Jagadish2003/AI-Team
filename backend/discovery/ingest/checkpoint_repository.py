"""R16-A1 / AT-378 — checkpoint persistence repository.

Read/write of the per-source ingestion checkpoint stored in the
``ingestion_checkpoints`` PostgreSQL table (one row per ``(org_id, connector_id)``).
This is the storage layer for the Change-Based Ingestion contract in
``discovery/ingest/base.py``.

The strict persistence rule (R16-A1 §2) is a division of responsibility:

  * THIS repository only does atomic reads and an atomic upsert. It never decides
    *when* to advance a checkpoint.
  * The RUNNER (AT-379) owns the lifecycle: read the checkpoint before a run,
    stream the delta, and call :func:`save_checkpoint` ONLY after the final batch
    (``is_complete=True``) has been fully processed. On any failure it simply does
    not call save, so the prior checkpoint stays put and the next run re-reads
    from the last known-good position (AC2).

``value`` is opaque — the repository persists and returns it verbatim and never
interprets it, so connectors using different shapes (an ISO timestamp, a commit
SHA, a sequence id) all share this one storage path (AC5).

DB access goes through the raw psycopg2 layer in ``app.db`` (imported lazily, the
same way ``discovery/runner.py`` does, to avoid an import cycle with the app
package at module load).
"""
from __future__ import annotations

from typing import Optional

from .base import Checkpoint


def _connect():
    # Lazy import: discovery is imported by the app package during route
    # registration, so importing app.db at module top could create a cycle.
    from app import db

    return db.connect()


def read_checkpoint(org_id: str, connector_id: str) -> Optional[Checkpoint]:
    """Return the stored checkpoint for ``(org_id, connector_id)``, or None.

    None means there is no row yet — a first run. The runner treats that as
    "no position" and performs an initial (streamed, resumable) full load.
    """
    con = _connect()
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT value, captured_at FROM ingestion_checkpoints "
            "WHERE org_id = %s AND connector_id = %s AND is_deleted = FALSE",
            (org_id, connector_id),
        )
        row = cur.fetchone()
        # Close the implicit read transaction cleanly. psycopg2 opens a
        # transaction on the SELECT (autocommit is off in app.db.connect());
        # commit it here so the connection is not left idle-in-transaction.
        con.commit()
    finally:
        con.close()
    if not row:
        return None
    value, captured_at = row[0], row[1]
    # captured_at comes back as a tz-aware datetime from PostgreSQL; the Checkpoint
    # contract carries it as an ISO string, so normalise here.
    captured_at_iso = captured_at.isoformat() if hasattr(captured_at, "isoformat") else str(captured_at)
    return Checkpoint(
        connector_id=connector_id,
        org_id=org_id,
        value=value,
        captured_at=captured_at_iso,
    )


def save_checkpoint(checkpoint: Checkpoint) -> None:
    """Atomically upsert a checkpoint row (one per ``(org_id, connector_id)``).

    Call this ONLY after a run has fully consumed its delta (the runner's
    write-only-on-full-success rule, AC2). ``value`` is stored verbatim — opaque
    to this layer. ``updated_at`` is refreshed by the DB on every write.
    """
    con = _connect()
    try:
        cur = con.cursor()
        cur.execute(
            "INSERT INTO ingestion_checkpoints "
            "(org_id, connector_id, value, captured_at, updated_at) "
            "VALUES (%s, %s, %s, %s, now()) "
            "ON CONFLICT (org_id, connector_id) DO UPDATE SET "
            "value = EXCLUDED.value, "
            "captured_at = EXCLUDED.captured_at, "
            "updated_at = now(), "
            "is_deleted = FALSE",  # re-saving reactivates a previously-reset row
            (
                checkpoint.org_id,
                checkpoint.connector_id,
                checkpoint.value,
                checkpoint.captured_at,
            ),
        )
        con.commit()
    except Exception:
        # Make the failure path explicit rather than relying on psycopg2's
        # implicit rollback on close. The exception propagates so the runner
        # knows the checkpoint did NOT advance (write-only-on-full-success).
        con.rollback()
        raise
    finally:
        con.close()


def reset_checkpoint(org_id: str, connector_id: str) -> bool:
    """Clear a source's checkpoint so the next run does a full re-read (AT-383).

    Soft-deletes the ``(org_id, connector_id)`` row (sets ``is_deleted = TRUE``),
    so a subsequent :func:`read_checkpoint` returns ``None`` — the "first run"
    path — and the connector performs a streamed full load. A later
    :func:`save_checkpoint` reactivates the row. This is an explicit,
    admin-initiated action (R16-A1 §3); it is never triggered automatically.

    Soft-delete (UPDATE) rather than a hard DELETE because the least-privilege
    application DB role has UPDATE but not DELETE — the same convention used for
    the other soft-deletable tables in this schema.

    Returns True if a live checkpoint existed and was cleared, False if there was
    nothing to clear (already reset, or never set — the "first run" state).
    """
    con = _connect()
    try:
        cur = con.cursor()
        cur.execute(
            "UPDATE ingestion_checkpoints SET is_deleted = TRUE, updated_at = now() "
            "WHERE org_id = %s AND connector_id = %s AND is_deleted = FALSE",
            (org_id, connector_id),
        )
        cleared = cur.rowcount
        con.commit()
    except Exception:
        # Explicit failure path, mirroring save_checkpoint(): on a failed
        # commit the row stays in its prior state and the error propagates.
        con.rollback()
        raise
    finally:
        con.close()
    return cleared > 0
