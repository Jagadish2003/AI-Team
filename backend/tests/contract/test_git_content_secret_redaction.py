"""Contract tests — Git content secret redaction over the real store (R18-A2 / AT-531).

End-to-end verification of AC5 against the ACTUAL pgvector-backed
``retrieval_chunks`` store (the discovery suite fakes the substrate):

  AC5 — Seeded secret patterns in content are redacted BEFORE indexing; the
        redaction is recorded; the secret is NOT retrievable. Here we drive the
        real ``GitContentIngestor`` (offline fixture, real substrate, no injected
        fns) over content that carries a seeded secret in BOTH streams — an
        API_KEY assignment in ``src/utils.py`` (code) and an AWS key in the
        ``data-pipeline`` d1 commit message (conversation). We then prove against
        the real store that (a) the content WAS indexed (so redaction did not just
        drop it), (b) no stored chunk and no retrieval hit contains the secret, and
        (c) the redaction was recorded as an ``ingestion.secret_redacted`` event.

Runs offline against ``git_content_sample.json``. Embedding is driven through a
FAKE provider registered with the real gateway (unique name, no registry collision).
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
from discovery.ingest.base import Checkpoint
from discovery.ingest.git_content import GitContentIngestor

# The exact seeded secrets that must NEVER reach the store or a retrieval result.
_FILE_SECRET = "sk-live-0123456789abcdef"          # src/utils.py  (API_KEY = ...)
_COMMIT_SECRET = "AKIAIOSFODNN7EXAMPLE"            # d1 commit message (AWS key)
_UTILS_ARTIFACT = "web-app:src/utils.py"
_D1_ARTIFACT = "data-pipeline@d1d1d1d1"


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


class _RedFakeProvider(ModelProvider):
    emits_own_telemetry = True

    def __init__(self, name: str):
        self.name = name

    def generate(self, req: GenerationRequest) -> GenerationResult:  # pragma: no cover
        return GenerationResult(text=None, provider=self.name, ok=False)

    def embed(self, texts: List[str]) -> List[List[float]]:
        return [[float(len(t) % 5) + 1.0, 0.5, 0.25, 0.125] for t in texts]

    def embedding_identity(self):
        return ("red:model-a", "1")


_RED_OK = _RedFakeProvider("at531_embed_ok")
register_provider(_RED_OK)


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
    monkeypatch.setenv("MODEL_EMBEDDING_PROVIDER", _RED_OK.name)
    monkeypatch.setenv("INGEST_MODE", "offline")
    name = f"ct_at531_{request.node.name}"[:60]
    _cleanup(name)
    yield name
    _cleanup(name)


def _rows_containing(org_id: str, needle: str) -> list:
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT chunk_id FROM retrieval_chunks "
            "WHERE org_id = %s AND content LIKE %s",
            (org_id, f"%{needle}%"),
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        con.close()


def _rows_for(org_id: str, source_artifact: str) -> list:
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT chunk_id, content FROM retrieval_chunks "
            "WHERE org_id = %s AND source_artifact = %s",
            (org_id, source_artifact),
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        con.close()


def test_ac5_seeded_secrets_are_redacted_before_indexing_and_not_retrievable(org):
    # 1. Drive the real ingestor over the fixture (offline, real substrate). A
    #    first run streams the HEAD tree AND the commit corpus through the
    #    unconditional secret scan before any hand-off to ingest_content.
    ing = GitContentIngestor()
    list(ing.ingest_changes(org, None))

    # 2. The content WAS indexed (redaction did not silently drop it) …
    utils_rows = _rows_for(org, _UTILS_ARTIFACT)
    d1_rows = _rows_for(org, _D1_ARTIFACT)
    assert utils_rows, "the file content should still be indexed (minus the secret)"
    assert d1_rows, "the commit message should still be indexed (minus the secret)"

    # … but the placeholder is present and the secret VALUE is gone.
    assert any("[REDACTED:" in r["content"] for r in utils_rows)
    assert any("[REDACTED:" in r["content"] for r in d1_rows)

    # 3. No stored chunk in this org contains either seeded secret (AC5 core).
    assert _rows_containing(org, _FILE_SECRET) == []
    assert _rows_containing(org, _COMMIT_SECRET) == []

    # 4. And the secret is not retrievable — embed, then query text that would
    #    surface the secret's neighbourhood; no hit content carries the secret.
    embedder.embed_pending_for_org(org)
    for query in ("API_KEY credentials token", "warehouse creds AWS access key"):
        for hit in retrieve(org, query, k=10):
            assert _FILE_SECRET not in hit.content
            assert _COMMIT_SECRET not in hit.content


def test_ac5_redaction_is_recorded(org, monkeypatch):
    events: list = []
    monkeypatch.setattr(
        "app.telemetry.record_event", lambda et, p=None: events.append((et, p))
    )
    ing = GitContentIngestor()
    list(ing.ingest_changes(org, None))

    red = {p["source_artifact"]: p for et, p in events if et == "ingestion.secret_redacted"}
    # Both seeded secrets were recorded, with pattern types and never the value.
    assert red[_UTILS_ARTIFACT]["pattern_types"] == ["secret_assignment"]
    assert red[_D1_ARTIFACT]["pattern_types"] == ["aws_access_key_id"]
    for payload in red.values():
        assert payload["connector_id"] == "git_content"
        assert payload["redaction_count"] >= 1
        assert _FILE_SECRET not in str(payload)
        assert _COMMIT_SECRET not in str(payload)
