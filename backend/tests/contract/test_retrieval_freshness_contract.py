"""Contract tests — retrieval freshness over the real store (R18-B2 T1).

End-to-end verification of the ingestion.artifact_changed subscriber against the
ACTUAL pgvector-backed ``retrieval_chunks`` store + ``retrieval_refresh_queue``
(the pure-logic suite fakes them). Proves the T1 slice of Section 1:

* AC1 (T1 side) — an updated artifact's chunks are marked ``is_stale = TRUE`` on
  the change event, and the artifact is queued for refresh.
* AC2 — a deleted artifact's chunks are removed from the store IMMEDIATELY on the
  deletion event; no row survives, so deleted content is no longer retrievable.
* Enqueue idempotency — repeated change events for one artifact collapse into a
  single pending queue row (cost proportional to change, not event volume).
* Deletion supersedes a pending refresh — a delete drops the queue row.
* The ingestion->retrieval field mapping (connector_id->source_system,
  artifact_id->source_artifact) resolves to the right stored rows.

The subscriber is driven exactly as production drives it — ``on_artifact_changed``
with the same dict shape the change runner emits.
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app import db
from app.retrieval import refresh_queue, store
from app.retrieval.freshness import on_artifact_changed
from database.models.retrieval import RetrievalChunkRecord


# ---------------------------------------------------------------------------
# Skip cleanly if this environment has no freshness schema. In CI the migration
# (0025) runs, so both the column and the queue table exist.
# ---------------------------------------------------------------------------


def _freshness_schema_available() -> bool:
    try:
        con = db.connect()
        try:
            cur = con.cursor()
            cur.execute("SELECT to_regclass('public.retrieval_refresh_queue')")
            if cur.fetchone()[0] is None:
                return False
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'retrieval_chunks' AND column_name = 'is_stale'"
            )
            return cur.fetchone() is not None
        finally:
            con.close()
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _freshness_schema_available(),
    reason="retrieval freshness schema (0025) not present in this environment",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cleanup(org_id: str) -> None:
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute("DELETE FROM retrieval_chunks WHERE org_id = %s", (org_id,))
        cur.execute("DELETE FROM retrieval_refresh_queue WHERE org_id = %s", (org_id,))
        con.commit()
    finally:
        con.close()


def _seed_chunks(org_id, source_system, source_artifact, n=3):
    """Insert n indexed chunks for one artifact (no embedding needed for T1)."""
    now = datetime.now(timezone.utc)
    records = [
        RetrievalChunkRecord(
            chunk_id=str(uuid4()),
            org_id=org_id,
            content=f"chunk {i} body text for {source_artifact}",
            content_type="prose",
            source_system=source_system,
            source_artifact=source_artifact,
            source_timestamp=now,
            chunk_position=i,
            provenance={"seeded": True},
        )
        for i in range(n)
    ]
    store.upsert_chunks(records)
    return records


def _stale_flags(org_id, source_artifact):
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT is_stale, stale_at FROM retrieval_chunks "
            "WHERE org_id = %s AND source_artifact = %s",
            (org_id, source_artifact),
        )
        return cur.fetchall()
    finally:
        con.close()


def _event(change_kind, org, connector="confluence", artifact="page/ops"):
    return {
        "org_id": org,
        "connector_id": connector,
        "artifact_id": artifact,
        "change_kind": change_kind,
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }


@pytest.fixture
def org(request):
    name = f"ct_fresh_{request.node.name}"[:60]
    _cleanup(name)
    yield name
    _cleanup(name)


# ---------------------------------------------------------------------------
# AC1 (T1 side) — updated marks chunks stale + queues a refresh
# ---------------------------------------------------------------------------


def test_ac1_updated_marks_chunks_stale_and_queues_refresh(org):
    _seed_chunks(org, "confluence", "page/ops", n=3)
    assert store.count_stale(org) == 0
    assert refresh_queue.pending_count(org) == 0

    on_artifact_changed(_event("updated", org, "confluence", "page/ops"))

    # Every chunk for the artifact is now stale, with a stale_at timestamp.
    flags = _stale_flags(org, "page/ops")
    assert len(flags) == 3
    assert all(is_stale for is_stale, _ in flags)
    assert all(stale_at is not None for _, stale_at in flags)
    assert store.count_stale(org) == 3

    # And the artifact is queued for asynchronous refresh.
    assert refresh_queue.pending_count(org) == 1
    pending = refresh_queue.fetch_pending(org)
    assert pending[0]["source_system"] == "confluence"
    assert pending[0]["source_artifact"] == "page/ops"
    assert pending[0]["status"] == "pending"


def test_created_event_queues_even_with_no_existing_chunks(org):
    # First-seen artifact: nothing indexed yet, but it must still be queued so the
    # refresh worker (T3) will index it.
    on_artifact_changed(_event("created", org, "git", "repo/new.py"))
    assert refresh_queue.pending_count(org) == 1
    assert store.count_stale(org) == 0  # nothing to mark yet


# ---------------------------------------------------------------------------
# AC2 — deleted removes chunks immediately
# ---------------------------------------------------------------------------


def test_ac2_deleted_removes_chunks_immediately(org):
    _seed_chunks(org, "confluence", "page/gone", n=4)
    assert len(_stale_flags(org, "page/gone")) == 4

    on_artifact_changed(_event("deleted", org, "confluence", "page/gone"))

    # No rows survive — deleted content is no longer retrievable, at all.
    assert _stale_flags(org, "page/gone") == []
    assert store.count_stale(org) == 0


def test_deletion_drops_a_pending_refresh_row(org):
    _seed_chunks(org, "confluence", "page/x", n=2)
    on_artifact_changed(_event("updated", org, "confluence", "page/x"))
    assert refresh_queue.pending_count(org) == 1

    # A later delete supersedes the queued refresh — nothing left to refresh.
    on_artifact_changed(_event("deleted", org, "confluence", "page/x"))
    assert refresh_queue.pending_count(org) == 0
    assert _stale_flags(org, "page/x") == []


# ---------------------------------------------------------------------------
# Cost proportional to change — repeat events collapse into one pending row
# ---------------------------------------------------------------------------


def test_repeat_change_events_collapse_into_one_pending_row(org):
    _seed_chunks(org, "slack", "thread/42", n=1)
    for _ in range(5):
        on_artifact_changed(_event("updated", org, "slack", "thread/42"))
    # Five events, one queued artifact.
    assert refresh_queue.pending_count(org) == 1


# ---------------------------------------------------------------------------
# Org isolation — one org's change never touches another's chunks
# ---------------------------------------------------------------------------


def test_change_is_org_scoped(org):
    other = org + "_other"
    _cleanup(other)
    try:
        _seed_chunks(org, "confluence", "page/shared-id", n=2)
        _seed_chunks(other, "confluence", "page/shared-id", n=2)

        on_artifact_changed(_event("updated", org, "confluence", "page/shared-id"))

        # Only the acting org's chunks go stale.
        assert store.count_stale(org) == 2
        assert store.count_stale(other) == 0
        assert refresh_queue.pending_count(other) == 0
    finally:
        _cleanup(other)
