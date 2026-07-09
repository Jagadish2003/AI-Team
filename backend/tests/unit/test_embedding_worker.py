"""Unit tests for the retrieval embedding worker (R18-B1 T3 + R18-B2 T5).

The worker is a thin, non-raising driver: each tick it drains the pending
``embedding IS NULL`` backlog (T3) and then runs the managed model-version backfill
(T5) that re-embeds old-model vectors onto the active model. These tests fake the
``embedder`` entry points so no PostgreSQL/gateway is needed and assert:

* both passes run, each bounded by its own per-org cap;
* a failure in either pass is swallowed (the scheduler thread must never crash) and
  does not prevent the other pass from running.
"""
from __future__ import annotations

from app.jobs import embedding_worker
from app.retrieval.embedder import EmbeddingRunResult, ModelBackfillResult


def test_worker_runs_pending_then_backfill(monkeypatch):
    calls = []

    def fake_pending(*, max_chunks_per_org=None):
        calls.append(("pending", max_chunks_per_org))
        return [EmbeddingRunResult(org_id="o1", embedded=2)]

    def fake_backfill(*, max_chunks_per_org=None):
        calls.append(("backfill", max_chunks_per_org))
        return [ModelBackfillResult(org_id="o1", reembedded=1)]

    monkeypatch.setattr(embedding_worker.embedder, "embed_pending_all_orgs", fake_pending)
    monkeypatch.setattr(
        embedding_worker.embedder, "backfill_stale_model_all_orgs", fake_backfill
    )

    embedding_worker.run_embedding_worker()

    assert [c[0] for c in calls] == ["pending", "backfill"]
    # Each pass is bounded by its own configured per-org cap.
    assert calls[0][1] == embedding_worker.EMBEDDING_WORKER_MAX_CHUNKS_PER_ORG
    assert calls[1][1] == embedding_worker.EMBEDDING_WORKER_BACKFILL_MAX_CHUNKS_PER_ORG


def test_pending_pass_failure_does_not_block_backfill(monkeypatch):
    calls = []

    def boom(*, max_chunks_per_org=None):
        raise RuntimeError("pending pass down")

    def fake_backfill(*, max_chunks_per_org=None):
        calls.append("backfill")
        return []

    monkeypatch.setattr(embedding_worker.embedder, "embed_pending_all_orgs", boom)
    monkeypatch.setattr(
        embedding_worker.embedder, "backfill_stale_model_all_orgs", fake_backfill
    )

    embedding_worker.run_embedding_worker()  # must not raise
    assert calls == ["backfill"]  # backfill still ran despite pending failure


def test_backfill_pass_failure_is_swallowed(monkeypatch):
    def fake_pending(*, max_chunks_per_org=None):
        return []

    def boom(*, max_chunks_per_org=None):
        raise RuntimeError("backfill pass down")

    monkeypatch.setattr(embedding_worker.embedder, "embed_pending_all_orgs", fake_pending)
    monkeypatch.setattr(embedding_worker.embedder, "backfill_stale_model_all_orgs", boom)

    embedding_worker.run_embedding_worker()  # must not raise
