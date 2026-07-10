"""Contract tests — freshness metrics queryable per org, end to end (R18-B2 T6 / AC7).

AC7: "Freshness metrics are queryable: pending change events, stale chunk count,
and backfill progress per org." Verified over the REAL pgvector-backed store and
refresh queue, through the REAL HTTP route the run-health dashboard will call:

* ``GET /api/retrieval/freshness`` serves the org's lag picture — pending change
  events, failed refreshes, stale chunks, embedding backlog, backfill progress —
  in the documented response shape;
* the numbers move with the actual machinery: T5 ingest raises the embedding
  backlog, T1-style stale marking + enqueue raises the pending/stale counts, the
  T3-worker-style completion drains them, a model repin flips backfill progress
  and the T5 backfill converges it back to complete;
* access posture: auth required, analyst+ role required, org strictly from the
  tenancy context — one org's lag is invisible to another.

Embedding is driven through FAKE providers registered with the real gateway and
selected via ``MODEL_EMBEDDING_PROVIDER`` (production path, no direct provider
call). Provider names are unique to this module.
"""
from __future__ import annotations

from typing import List

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app
from app.model_gateway import register_provider
from app.model_gateway._interface import (
    GenerationRequest,
    GenerationResult,
    ModelProvider,
)
from app.rbac import seed_owner
from app.retrieval import embedder, refresh_queue, store
from app.retrieval.ingest import ingest_content

_DEV_TOKEN = "dev-token-change-me"

client = TestClient(app)


# ---------------------------------------------------------------------------
# Skip cleanly if this environment has no pgvector-backed store. In CI the
# migration runs.
# ---------------------------------------------------------------------------


def _retrieval_store_available() -> bool:
    try:
        con = db.connect()
        try:
            cur = con.cursor()
            cur.execute("SELECT to_regclass('public.retrieval_chunks')")
            return cur.fetchone()[0] is not None
        finally:
            con.close()
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _retrieval_store_available(),
    reason="retrieval_chunks store (pgvector) not present in this environment",
)


# ---------------------------------------------------------------------------
# Fake embedding providers registered with the real gateway
# ---------------------------------------------------------------------------


class _T6MFakeProvider(ModelProvider):
    emits_own_telemetry = True

    def __init__(self, name: str, identity):
        self.name = name
        self._identity = identity

    def generate(self, req: GenerationRequest) -> GenerationResult:  # pragma: no cover
        return GenerationResult(text=None, provider=self.name, ok=False)

    def embed(self, texts: List[str]) -> List[List[float]]:
        return [[float(len(t) % 5) + 1.0, 0.5, 0.25, 0.125] for t in texts]

    def embedding_identity(self):
        return self._identity


_T6M_A = _T6MFakeProvider("t6m_embed_a", ("t6m:model-a", "1"))
_T6M_B = _T6MFakeProvider("t6m_embed_b", ("t6m:model-b", "2"))

for _p in (_T6M_A, _T6M_B):
    register_provider(_p)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _auth(org_id: str) -> dict:
    """Headers authenticating the dev user as ``org_id`` (dev token carries no
    signed org claim, so X-Org-Id drives the tenancy context in tests)."""
    return {"Authorization": f"Bearer {_DEV_TOKEN}", "X-Org-Id": org_id}


def _cleanup(org_id: str) -> None:
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute("DELETE FROM retrieval_chunks WHERE org_id = %s", (org_id,))
        cur.execute(
            "DELETE FROM retrieval_refresh_queue WHERE org_id = %s", (org_id,)
        )
        con.commit()
    finally:
        con.close()


@pytest.fixture
def org(request, monkeypatch):
    """A unique, owner-seeded org per test on the good provider; cleaned after."""
    monkeypatch.setenv("MODEL_EMBEDDING_PROVIDER", _T6M_A.name)
    name = f"ct_t6m_{request.node.name}"[:60]
    seed_owner(name, _DEV_TOKEN)
    _cleanup(name)
    yield name
    _cleanup(name)


def _get_metrics(org_id: str) -> dict:
    resp = client.get("/api/retrieval/freshness", headers=_auth(org_id))
    assert resp.status_code == 200, resp.text
    return resp.json()


def _ingest(org_id: str, n: int = 3) -> int:
    result = ingest_content(
        org_id,
        [
            dict(
                source_system="confluence",
                source_artifact=f"page/{i}",
                content=f"Operations paragraph {i}: approvals wait on manual review.",
                content_type="prose",
            )
            for i in range(n)
        ],
    )
    assert result.artifacts_indexed == n
    return result.chunks_indexed


# ---------------------------------------------------------------------------
# AC7 — the three required signals are queryable and move with the machinery
# ---------------------------------------------------------------------------


def test_ac7_empty_org_reads_all_clear(org):
    out = _get_metrics(org)
    assert out["org_id"] == org
    assert out["pending_change_events"] == 0
    assert out["failed_refreshes"] == 0
    assert out["stale_chunks"] == 0
    assert out["chunks_total"] == 0
    assert out["pending_embeddings"] == 0
    assert out["backfill"]["embedded_total"] == 0
    assert out["backfill"]["progress"] == 1.0
    assert out["backfill"]["complete"] is True
    assert out["generated_at"]


def test_ac7_pending_events_and_stale_chunks_are_counted(org):
    chunks = _ingest(org, n=3)

    # Change events arrive for one artifact: its chunks go stale and it queues
    # for refresh (the T1 invalidation contract).
    store.mark_stale(org, "confluence", "page/0")
    refresh_queue.enqueue(org, "confluence", "page/0")

    out = _get_metrics(org)
    assert out["pending_change_events"] == 1
    assert out["stale_chunks"] >= 1
    assert out["chunks_total"] == chunks
    assert out["pending_embeddings"] == chunks  # nothing embedded yet

    # The refresh completes (worker path): the re-ingest replaces the artifact's
    # chunks (fresh rows are never stale) and the queue row is removed — the lag
    # drains back to zero.
    ingest_content(
        org,
        [dict(source_system="confluence", source_artifact="page/0",
              content="Refreshed page zero content.", content_type="prose")],
    )
    row_ids = refresh_queue.fetch_pending(org, limit=1)
    if row_ids:
        refresh_queue.mark_done(org, row_ids[0]["id"])
    out2 = _get_metrics(org)
    assert out2["pending_change_events"] == 0
    assert out2["stale_chunks"] == 0


def test_ac7_failed_refreshes_are_visible_not_hidden(org):
    _ingest(org, n=1)
    qid = refresh_queue.enqueue(org, "confluence", "page/0")
    status = refresh_queue.mark_failed(org, qid, "extractor exploded", max_attempts=1)
    assert status == "failed"

    out = _get_metrics(org)
    assert out["failed_refreshes"] == 1
    assert out["pending_change_events"] == 0  # parked, no longer pending


def test_ac7_backfill_progress_tracks_a_model_repin(org, monkeypatch):
    chunks = _ingest(org, n=2)
    run = embedder.embed_pending_for_org(org)
    assert run.embedded == chunks

    # Everything embedded under model A: backlog empty, backfill complete.
    out = _get_metrics(org)
    assert out["pending_embeddings"] == 0
    assert out["backfill"]["active_model"] == "t6m:model-a"
    assert out["backfill"]["embedded_total"] == chunks
    assert out["backfill"]["on_active_model"] == chunks
    assert out["backfill"]["progress"] == 1.0
    assert out["backfill"]["complete"] is True

    # Repin to model B: every vector is now old-generation — progress collapses
    # to 0.0 and the metric says so instead of hiding it.
    monkeypatch.setenv("MODEL_EMBEDDING_PROVIDER", _T6M_B.name)
    out_repin = _get_metrics(org)
    assert out_repin["backfill"]["active_model"] == "t6m:model-b"
    assert out_repin["backfill"]["awaiting_backfill"] == chunks
    assert out_repin["backfill"]["on_active_model"] == 0
    assert out_repin["backfill"]["progress"] == 0.0
    assert out_repin["backfill"]["complete"] is False

    # The managed backfill (T5) converges the partition onto model B and the
    # metric follows it back to complete.
    result = embedder.backfill_stale_model_for_org(org)
    assert result.reembedded == chunks
    out_done = _get_metrics(org)
    assert out_done["backfill"]["awaiting_backfill"] == 0
    assert out_done["backfill"]["progress"] == 1.0
    assert out_done["backfill"]["complete"] is True


# ---------------------------------------------------------------------------
# Per org — one tenant's lag is invisible to another
# ---------------------------------------------------------------------------


def test_ac7_metrics_are_org_scoped(org):
    _ingest(org, n=2)
    store.mark_stale(org, "confluence", "page/0")
    refresh_queue.enqueue(org, "confluence", "page/0")

    other = f"{org}_other"[:60]
    seed_owner(other, _DEV_TOKEN)
    _cleanup(other)
    try:
        out_other = _get_metrics(other)
        assert out_other["org_id"] == other
        assert out_other["pending_change_events"] == 0
        assert out_other["stale_chunks"] == 0
        assert out_other["chunks_total"] == 0
    finally:
        _cleanup(other)


# ---------------------------------------------------------------------------
# Access posture
# ---------------------------------------------------------------------------


def test_freshness_requires_auth():
    resp = client.get("/api/retrieval/freshness")
    assert resp.status_code == 401


def test_freshness_requires_analyst_role():
    # Seed the dev token as a VIEWER in a fresh org (the test_rbac_enforcement
    # pattern); the analyst+ gate on /api/retrieval must reject it.
    import uuid
    from datetime import datetime, timezone

    from app.rbac import _ensure_members_table

    _ensure_members_table()
    viewer_org = f"ct_t6m_viewer_{uuid.uuid4().hex[:8]}"
    con = db.connect()
    try:
        con.execute(
            "INSERT INTO workspace_members (org_id, user_id, role, created_at) "
            "VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (org_id, user_id) DO UPDATE SET role=EXCLUDED.role",
            (viewer_org, _DEV_TOKEN, "viewer",
             datetime.now(timezone.utc).isoformat()),
        )
        con.commit()
    finally:
        con.close()

    resp = client.get("/api/retrieval/freshness", headers=_auth(viewer_org))
    assert resp.status_code == 403
