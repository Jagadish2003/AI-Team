"""Contract tests — Git content deletion propagation over the real store (R18-A2 / AT-533).

End-to-end verification of AC3 against the ACTUAL pgvector-backed
``retrieval_chunks`` store (the discovery suite fakes the substrate):

  AC3 — A file deleted in a commit emits a ``change_kind='deleted'`` event AND its
        chunks leave retrieval. Here we prove the second half against the real
        store: a git file that was indexed and retrievable is deleted by the next
        commit, the ``GitContentIngestor`` routes that deletion to the substrate's
        freshness removal, and the file's chunks are gone from the store and no
        longer returned by ``retrieve()`` — while the org's other content and
        another org's identical-path content are untouched.

Runs offline against the ``git_content_sample.json`` fixture (whose ``web-app``
c1..c3 diff deletes ``src/legacy.py``). Embedding is driven through a FAKE
provider registered with the real gateway (unique name, no registry collision).
"""
from __future__ import annotations

from typing import List

import pytest

from app import db
from app.model_gateway import register_provider
from app.model_gateway._interface import (
    GenerationRequest,
    GenerationResult,
    ModelProvider,
)
from app.retrieval import embedder, store
from app.retrieval.api import retrieve
from app.retrieval.ingest import ContentArtifact, ingest_content, remove_content
from discovery.ingest.base import Checkpoint
from discovery.ingest.git_content import GitContentIngestor, _encode_checkpoint


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


class _DelFakeProvider(ModelProvider):
    emits_own_telemetry = True

    def __init__(self, name: str):
        self.name = name

    def generate(self, req: GenerationRequest) -> GenerationResult:  # pragma: no cover
        return GenerationResult(text=None, provider=self.name, ok=False)

    def embed(self, texts: List[str]) -> List[List[float]]:
        return [[float(len(t) % 5) + 1.0, 0.5, 0.25, 0.125] for t in texts]

    def embedding_identity(self):
        return ("del:model-a", "1")


_DEL_OK = _DelFakeProvider("at533_embed_ok")
register_provider(_DEL_OK)


def _cleanup(org_id: str) -> None:
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute("DELETE FROM retrieval_chunks WHERE org_id = %s", (org_id,))
        con.commit()
    finally:
        con.close()


@pytest.fixture
def org(request, monkeypatch):
    monkeypatch.setenv("MODEL_EMBEDDING_PROVIDER", _DEL_OK.name)
    # Ensure the git ingestor reads the offline fixture, not a live clone.
    monkeypatch.setenv("INGEST_MODE", "offline")
    name = f"ct_at533_{request.node.name}"[:60]
    _cleanup(name)
    yield name
    _cleanup(name)


_LEGACY_ARTIFACT = dict(
    source_system="git",
    source_artifact="web-app:src/legacy.py",
    content="def legacy_handler(evt):\n    return process(evt)\n",
    content_type="code",
    source_timestamp="2026-06-10T09:00:00+00:00",
    provenance={"repo": "web-app", "path": "src/legacy.py", "commit_sha": "c1c1c1c1"},
)


def _incremental_since() -> Checkpoint:
    # web-app synced at c1 (before the deletion); data-pipeline already at HEAD.
    return Checkpoint.create(
        "git_content",
        "org",
        _encode_checkpoint(
            {
                "web-app": {"sha": "c1c1c1c1", "offset": None},
                "data-pipeline": {"sha": "d2d2d2d2", "offset": None},
            }
        ),
    )


def test_ac3_deleted_git_file_chunks_leave_retrieval(org):
    # 1. The file existed at c1: index it and embed it so it is retrievable.
    ingest_content(org, [dict(_LEGACY_ARTIFACT)])
    assert store.count_chunks(org) >= 1
    embedder.embed_pending_for_org(org)
    hits = retrieve(org, "legacy handler process event", k=5)
    assert any(h.source_artifact == "web-app:src/legacy.py" for h in hits)

    # 2. The c1..c3 diff deletes src/legacy.py. Drive the real ingestor (offline
    #    fixture, real substrate) — no injected fns, so it calls remove_content.
    ing = GitContentIngestor()
    since = Checkpoint.create("git_content", org, _incremental_since().value)
    list(ing.ingest_changes(org, since))

    # 3. The deleted file's chunks are gone from the store...
    legacy_rows = _rows_for(org, "web-app:src/legacy.py")
    assert legacy_rows == []
    # ...and it is no longer retrievable.
    embedder.embed_pending_for_org(org)  # embed the freshly-ingested changed files
    hits_after = retrieve(org, "legacy handler process event", k=10)
    assert all(h.source_artifact != "web-app:src/legacy.py" for h in hits_after)

    # The other files the same diff touched WERE (re)indexed — deletion is scoped.
    assert _rows_for(org, "web-app:src/main.py")
    assert _rows_for(org, "web-app:src/new_feature.py")


def test_ac3_removal_is_org_scoped(org):
    other = org + "_other"
    _cleanup(other)
    try:
        # Both orgs index the same git path.
        ingest_content(org, [dict(_LEGACY_ARTIFACT)])
        ingest_content(other, [dict(_LEGACY_ARTIFACT)])

        # remove_content for `org` only.
        remove_content(org, [("git", "web-app:src/legacy.py")])

        assert _rows_for(org, "web-app:src/legacy.py") == []
        assert _rows_for(other, "web-app:src/legacy.py")  # untouched
    finally:
        _cleanup(other)


def _rows_for(org_id: str, source_artifact: str) -> list[dict]:
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT chunk_id FROM retrieval_chunks "
            "WHERE org_id = %s AND source_artifact = %s",
            (org_id, source_artifact),
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        con.close()
