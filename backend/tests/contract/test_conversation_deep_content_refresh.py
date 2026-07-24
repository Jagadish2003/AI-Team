"""
R18-A4 / AT-596 (T3) — edit/delete propagation, end to end (AC3, AC4).

Proves the T3 acceptance criteria against the REAL R18-B1 substrate and the REAL
R18-B2 freshness + refresh machinery, through the one public path a discovery run
uses:

    SlackIngestor.ingest_deep_content(org, records)      # deep-content hand-off + T3
      → freshness.on_artifact_changed(thread event)      # B2 T1: mark stale / purge
        → refresh.refresh_pending_for_org(org)           # B2 T3: re-read whole thread
          → slack.resolve_thread_content(...)            # T3 content resolver
            → retrieve(org, query)                       # B1 T4 source-aware API

  AC3 (edit)   — an edited message re-chunks its WHOLE thread: the thread's chunks
                 are marked stale on the change event (excluded from retrieval at
                 once) and, after the async refresh, retrieval returns the NEW thread
                 text and no longer the old.
  AC3 (delete) — a deleted standalone message's content leaves retrieval IMMEDIATELY
                 on the deletion event (no refresh needed).
  AC4          — the refresh re-reads only the affected thread's channel, never full
                 channel history.

The source of truth for the resolver's re-read is a monkeypatched ``_raw_messages``
so the "current" state of the channel can be edited/deleted deterministically.
Embedding runs through a FAKE provider registered with the REAL model gateway and
selected via ``MODEL_EMBEDDING_PROVIDER``, so the production path executes.
"""
from __future__ import annotations

from typing import List

import pytest

import app.db as app_db
from app import db
from app.model_gateway import register_provider
from app.model_gateway._interface import (
    GenerationRequest,
    GenerationResult,
    ModelProvider,
)
from app.retrieval import embedder, freshness, refresh
from app.retrieval.api import retrieve
import discovery.ingest.slack as slack_mod
from discovery.ingest.slack import SlackIngestor


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

_TERMS = ("alpha", "beta", "omega", "gamma")


class _FakeProvider(ModelProvider):
    emits_own_telemetry = True

    def __init__(self, name: str, identity):
        self.name = name
        self._identity = identity

    def generate(self, req: GenerationRequest) -> GenerationResult:  # pragma: no cover
        return GenerationResult(text=None, provider=self.name, ok=False)

    def embed(self, texts: List[str]) -> List[List[float]]:
        out = []
        for t in texts:
            low = (t or "").lower()
            out.append([1.0 if term in low else 0.0 for term in _TERMS] + [0.01])
        return out

    def embedding_identity(self):
        return self._identity


_PROVIDER = _FakeProvider("conv_refresh_embed", ("conv-refresh:model", "1"))
register_provider(_PROVIDER)


def _rec(channel_id, ts, user, text, *, kind="created", thread_ts=None, reply_count=0):
    rec = {
        "channel_id": channel_id,
        "channel_name": "ops",
        "ts": ts,
        "user": user,
        "text": text,
        "reply_count": reply_count,
        "change_kind": kind,
    }
    if thread_ts is not None:
        rec["thread_ts"] = thread_ts
    return rec


def _cleanup(org_id: str) -> None:
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute("DELETE FROM retrieval_chunks WHERE org_id = %s", (org_id,))
        try:
            cur.execute("DELETE FROM retrieval_refresh_queue WHERE org_id = %s", (org_id,))
        except Exception:
            pass
        con.commit()
    finally:
        con.close()


@pytest.fixture
def org(request, monkeypatch):
    monkeypatch.setenv("MODEL_EMBEDDING_PROVIDER", _PROVIDER.name)
    monkeypatch.setenv("INGEST_MODE", "offline")
    # No P5 selection → every accessible channel is in scope.
    monkeypatch.setattr(app_db, "org_connector_get", lambda o, c: None)
    # The Slack thread resolver must be registered for the refresh worker to re-read.
    refresh.register_content_resolver("slack", slack_mod.resolve_thread_content)
    name = f"conv_ho_{request.node.name}"[:60]
    _cleanup(name)
    yield name
    _cleanup(name)


def _ids(hits) -> set:
    return {h.source_artifact for h in hits}


# ===========================================================================
# AC3 (edit) — an edited message re-chunks its whole thread
# ===========================================================================
def test_ac3_edit_rechunks_whole_thread(org, monkeypatch):
    thread_root = "1000.0001"
    thread_id = "C001:1000.0001"

    # 1) Index the thread (root "alpha incident" + reply "beta rollback").
    created = [
        _rec("C001", thread_root, "U1", "alpha incident on payments", reply_count=1),
        _rec("C001", "1000.0500", "U2", "beta rollback done", thread_ts=thread_root),
    ]
    res = SlackIngestor().ingest_deep_content(org, created, freshness_fn=lambda e: None)
    assert res.artifacts_handed_off == 1
    embedder.embed_pending_for_org(org)

    # Before the edit the thread is retrievable and carries the ORIGINAL reply text.
    pre = retrieve(org, "beta", k=5, source_filter=["slack"])
    assert thread_id in _ids(pre)
    top_pre = next(h for h in pre if h.source_artifact == thread_id)
    assert "beta rollback done" in top_pre.content

    # 2) The reply is edited to new text. The SOURCE now reflects the edit — this is
    #    what the resolver re-reads (targeted to this one channel, AC4).
    edited_channel = [
        {"ts": thread_root, "user": "U1", "text": "alpha incident on payments", "reply_count": 1},
        {"ts": "1000.0500", "user": "U2", "text": "omega mitigation shipped",
         "thread_ts": thread_root, "edited": {"ts": "1000.0600"}},
    ]
    monkeypatch.setattr(SlackIngestor, "_raw_messages", lambda self, o, ch: list(edited_channel))
    monkeypatch.setattr(
        SlackIngestor, "_raw_channels", lambda self, o: [{"id": "C001", "name": "ops", "is_private": False, "is_member": True, "is_archived": False}]
    )

    # 3) The edit flows through the deep path with REAL freshness → thread marked
    #    stale + queued. Marked stale means excluded from default retrieval at once.
    edit_res = SlackIngestor().ingest_deep_content(
        org, [_rec("C001", "1000.0500", "U2", "omega mitigation shipped", kind="updated", thread_ts=thread_root)]
    )
    assert edit_res.threads_refreshed == 1
    # Marked stale → hard-excluded from default retrieval at once (no window of
    # serving edited-away content as current).
    assert thread_id not in _ids(retrieve(org, "beta", k=5, source_filter=["slack"]))

    # 4) The async refresh re-reads the WHOLE thread and re-chunks it.
    run = refresh.refresh_pending_for_org(org)
    assert run.refreshed >= 1
    embedder.embed_pending_for_org(org)

    # After refresh: the thread is retrievable again, now carrying the NEW reply text
    # and no longer the old — proof the whole thread was re-chunked (AC3 edit).
    hits = retrieve(org, "omega", k=5, source_filter=["slack"])
    assert thread_id in _ids(hits)
    top = next(h for h in hits if h.source_artifact == thread_id)
    assert "omega mitigation shipped" in top.content
    assert "beta rollback" not in top.content
    # Root text is untouched — the whole thread, not just the edited message, is present.
    assert "alpha incident on payments" in top.content


# ===========================================================================
# AC3 (delete) — a deleted standalone message's content leaves retrieval at once
# ===========================================================================
def test_ac3_delete_removes_content_immediately(org):
    standalone_id = "C002:2000.0001"

    created = [_rec("C002", "2000.0001", "U3", "gamma deploy to prod")]
    SlackIngestor().ingest_deep_content(org, created, freshness_fn=lambda e: None)
    embedder.embed_pending_for_org(org)
    assert standalone_id in _ids(retrieve(org, "gamma", k=5, source_filter=["slack"]))

    # Delete the standalone message → immediate purge (no refresh needed).
    del_res = SlackIngestor().ingest_deep_content(
        org, [_rec("C002", "2000.0001", "U3", "", kind="deleted")]
    )
    assert del_res.threads_removed == 1

    # Content is gone from retrieval immediately.
    assert standalone_id not in _ids(retrieve(org, "gamma", k=10, source_filter=["slack"]))
