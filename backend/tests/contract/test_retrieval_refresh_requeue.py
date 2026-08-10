"""Contract tests for ``refresh_queue.requeue_failed`` (retrieval freshness).

A refresh row that exhausts its retry budget is parked ``failed``, and a parked row
is otherwise UNREACHABLE: both ``fetch_pending`` and ``orgs_with_pending`` filter on
``status = 'pending'``, and the only ``failed -> pending`` transition in the module is
the ``ON CONFLICT`` upsert in ``enqueue`` / ``store.mark_stale_and_enqueue`` — which
fires only on a NEW ``artifact_changed`` event. A checkpoint-based connector emits no
event for an unchanged artifact, so rows parked by a resolver defect stay parked, and
their chunks stay stale, indefinitely. Fixing the resolver does not revive them.

These tests pin the two properties an operator depends on when running the repair:
the row really becomes claimable again, and its retry budget is genuinely reset (a
row revived at the ceiling would re-park on its first transient failure, looking
exactly like the fix not working).
"""
from __future__ import annotations

import pytest

from app.retrieval import refresh_queue


ORG = "requeue_contract_org"
OTHER_ORG = "requeue_contract_other_org"


def _park(org_id: str, source_system: str, source_artifact: str) -> str:
    """Enqueue an artifact and drive it to ``failed`` with a 1-attempt budget."""
    queue_id = refresh_queue.enqueue(org_id, source_system, source_artifact)
    status = refresh_queue.mark_failed(org_id, queue_id, "no_content", max_attempts=1)
    assert status == "failed", f"expected the row to park, got {status!r}"
    return queue_id


@pytest.fixture(autouse=True)
def _clean():
    def _purge():
        for org in (ORG, OTHER_ORG):
            for system, artifact in (
                ("sharepoint", "S-eng:page:pg-1"),
                ("sharepoint", "S-eng:list:lst-1"),
                ("confluence", "ENG:100"),
            ):
                refresh_queue.remove(org, system, artifact)

    _purge()
    yield
    _purge()


def test_parked_row_is_invisible_to_the_worker_until_requeued():
    """The premise: a failed row is not returned by fetch_pending, so the worker
    will never retry it however many times it runs."""
    _park(ORG, "sharepoint", "S-eng:page:pg-1")

    assert refresh_queue.failed_count(ORG) == 1
    pending_ids = {r["source_artifact"] for r in refresh_queue.fetch_pending(ORG)}
    assert "S-eng:page:pg-1" not in pending_ids

    assert refresh_queue.requeue_failed(ORG) == 1

    pending = {r["source_artifact"]: r for r in refresh_queue.fetch_pending(ORG)}
    assert "S-eng:page:pg-1" in pending
    assert refresh_queue.failed_count(ORG) == 0


def test_requeue_resets_the_retry_budget():
    """``attempts`` must go back to 0. Neither upsert resets it, so a row revived at
    the ceiling gets exactly one attempt and re-parks on any transient failure —
    indistinguishable, to an operator, from the repair not having worked."""
    _park(ORG, "sharepoint", "S-eng:page:pg-1")
    refresh_queue.requeue_failed(ORG)

    row = next(
        r for r in refresh_queue.fetch_pending(ORG)
        if r["source_artifact"] == "S-eng:page:pg-1"
    )
    assert row["attempts"] == 0
    assert row["status"] == "pending"


def test_requeue_can_be_narrowed_to_one_source_system():
    """A repair targets the connector that was broken; unrelated parked rows (which
    may be parked for a reason that is still true) are left alone."""
    _park(ORG, "sharepoint", "S-eng:page:pg-1")
    _park(ORG, "confluence", "ENG:100")

    assert refresh_queue.requeue_failed(ORG, "sharepoint") == 1

    pending = {r["source_artifact"] for r in refresh_queue.fetch_pending(ORG)}
    assert "S-eng:page:pg-1" in pending
    assert "ENG:100" not in pending           # still parked
    assert refresh_queue.failed_count(ORG) == 1


def test_requeue_is_org_scoped():
    """Org scoping is in the SQL, consistent with the rest of the store."""
    _park(ORG, "sharepoint", "S-eng:page:pg-1")
    _park(OTHER_ORG, "sharepoint", "S-eng:page:pg-1")

    assert refresh_queue.requeue_failed(ORG) == 1

    assert refresh_queue.failed_count(ORG) == 0
    assert refresh_queue.failed_count(OTHER_ORG) == 1


def test_requeue_with_nothing_parked_is_a_no_op():
    """Safe to run repeatedly — a repair an operator reruns must not disturb rows
    that are pending and working through the queue normally."""
    refresh_queue.enqueue(ORG, "sharepoint", "S-eng:list:lst-1")

    assert refresh_queue.requeue_failed(ORG) == 0

    pending = {r["source_artifact"] for r in refresh_queue.fetch_pending(ORG)}
    assert "S-eng:list:lst-1" in pending      # untouched, still claimable


def test_requeue_never_deletes():
    """The queue's record of what failed and why survives the repair — requeue is an
    in-place UPDATE, not a delete-and-recreate."""
    _park(ORG, "sharepoint", "S-eng:page:pg-1")
    before = refresh_queue.enqueue(ORG, "sharepoint", "S-eng:page:pg-1")

    # Re-enqueue returns the SAME row id (idempotent upsert), proving the row was
    # never dropped; requeue_failed operates on that same row.
    refresh_queue.requeue_failed(ORG)
    after = next(
        r for r in refresh_queue.fetch_pending(ORG)
        if r["source_artifact"] == "S-eng:page:pg-1"
    )
    assert after["id"] == before
