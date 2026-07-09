"""R18-B2 T7 — the Retrieval Freshness acceptance suite (Section 3, AC1–AC7).

The freshness contract, pinned END TO END at the behaviour level so future
changes cannot silently break it. Every test drives the REAL machinery — the
``ingestion.artifact_changed`` subscriber (T1/T2), the durable refresh queue,
the async refresh worker with a registered content resolver (T3), the
pgvector-backed store, the gateway-routed embedder (fake providers registered
with the REAL gateway), ``retrieve()`` (T4), the context assembler (AC6), and
the freshness-metrics HTTP route (T6) — never internal shortcuts:

* AC1 — an updated artifact's chunks are marked stale ON THE CHANGE EVENT and
  excluded from default retrieval until the refresh completes.
* AC2 — a deleted artifact's chunks are removed IMMEDIATELY on the deletion
  event: no window where deleted content is still retrievable as current, no
  orphaned refresh-queue row.
* AC3 — refresh re-embeds ONLY chunks whose content hash actually changed;
  unchanged chunks carry their stored vector over with zero gateway calls.
* AC4 — chunk replacement is atomic at the artifact level: after a refresh,
  retrieval sees exactly the new chunk set — never a mix of old and new.
* AC5 — an embedding provider/version switch invalidates old-model vectors;
  retrieval never compares across model generations; the managed backfill
  re-embeds everything under the new model.
* AC6 — stale exclusion is a POLICY decision recorded on the assembly
  selection log as ``excluded: stale`` — visible, never silent.
* AC7 — freshness metrics are queryable per org over HTTP: pending change
  events, stale chunk count, and backfill progress.

Fake embedding providers are selected via ``MODEL_EMBEDDING_PROVIDER`` so the
production path (``get_embedding_provider()`` / ``model_gateway.embed``) is what
runs; provider names are unique to this module. The change events enter through
``freshness.on_artifact_changed`` with the raw ingestion payload vocabulary
(``org_id`` / ``connector_id`` / ``artifact_id`` / ``change_kind``) — exactly
what the connectors have emitted since 1.6.
"""
from __future__ import annotations

import uuid
from typing import List

import pytest
from fastapi.testclient import TestClient

from app import db
from app.context_assembly import REASON_STALE, AssemblyPolicy, assemble_context
from app.main import app
from app.model_gateway import register_provider
from app.model_gateway._interface import (
    GenerationRequest,
    GenerationResult,
    ModelProvider,
)
from app.rbac import seed_owner
from app.retrieval import embedder, freshness, refresh, refresh_queue, store
from app.retrieval.api import retrieve
from app.retrieval.evidence_source import retrieval_evidence_source
from app.retrieval.ingest import ingest_content

_DEV_TOKEN = "dev-token-change-me"
_SOURCE = "confluence"  # the connector_id the change events speak

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
# Fake embedding providers registered with the real gateway. Model A / model B
# are distinct generations for AC5; every embed() call is recorded so AC3 can
# count exactly which texts were (re-)embedded.
# ---------------------------------------------------------------------------


class _T7Provider(ModelProvider):
    emits_own_telemetry = True

    def __init__(self, name: str, identity):
        self.name = name
        self._identity = identity
        self.embed_calls: List[List[str]] = []

    def generate(self, req: GenerationRequest) -> GenerationResult:  # pragma: no cover
        return GenerationResult(text=None, provider=self.name, ok=False)

    def embed(self, texts: List[str]) -> List[List[float]]:
        self.embed_calls.append(list(texts))
        return [[float(len(t) % 5) + 1.0, 0.5, 0.25, 0.125] for t in texts]

    def embedding_identity(self):
        return self._identity


_T7_A = _T7Provider("t7_embed_a", ("t7:model-a", "1"))
_T7_B = _T7Provider("t7_embed_b", ("t7:model-b", "2"))

for _p in (_T7_A, _T7_B):
    register_provider(_p)


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


@pytest.fixture
def org(request, monkeypatch):
    """Unique org per test on provider A; queue/store cleaned before and after."""
    monkeypatch.setenv("MODEL_EMBEDDING_PROVIDER", _T7_A.name)
    name = f"ct_t7_{request.node.name}"[:60]
    _cleanup(name)
    yield name
    _cleanup(name)


@pytest.fixture
def source_content():
    """A mutable dict standing in for the source system's CURRENT content.

    Registered as the ``confluence`` content resolver (the T3 re-extraction
    seam): the refresh worker re-extracts whatever the dict currently maps an
    artifact id to. Tests mutate it to simulate the source changing.
    """
    contents: dict[str, str] = {}

    def _resolver(org_id: str, artifact_id: str):
        text = contents.get(artifact_id)
        if text is None:
            return None
        return dict(
            source_system=_SOURCE,
            source_artifact=artifact_id,
            content=text,
            content_type="prose",
        )

    refresh.register_content_resolver(_SOURCE, _resolver)
    yield contents
    refresh.clear_content_resolvers()


def _change_event(org_id: str, artifact_id: str, kind: str) -> None:
    """Deliver an ``ingestion.artifact_changed`` event exactly as emitted since
    1.6: the raw telemetry payload with the ingestion vocabulary."""
    freshness.on_artifact_changed(
        {
            "org_id": org_id,
            "connector_id": _SOURCE,
            "artifact_id": artifact_id,
            "change_kind": kind,
        }
    )


def _ingest_and_embed(org_id: str, artifact_id: str, content: str) -> int:
    """Producer handover (T5) + embedding pass: the artifact becomes current,
    retrievable evidence. Returns the number of chunks indexed."""
    result = ingest_content(
        org_id,
        [dict(source_system=_SOURCE, source_artifact=artifact_id,
              content=content, content_type="prose")],
    )
    assert result.artifacts_indexed == 1
    run = embedder.embed_pending_for_org(org_id)
    assert run.embedded >= result.chunks_indexed
    return result.chunks_indexed


def _hits(org_id: str, query: str = "paragraph", **kwargs):
    return retrieve(org_id, query, k=20, **kwargs)


def _artifacts_in(hits) -> set:
    return {h.source_artifact for h in hits}


def _rows(org_id: str, artifact_id: str) -> list[dict]:
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT chunk_id, content, content_hash, is_stale, "
            "       embedding_model, embedding_model_version "
            "FROM retrieval_chunks "
            "WHERE org_id = %s AND source_artifact = %s "
            "ORDER BY chunk_position",
            (org_id, artifact_id),
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        con.close()


# Three ~1.3k-char paragraphs: each exceeds half the chunk bound, so the prose
# policy yields one chunk per paragraph — a stable 3-chunk artifact whose
# paragraphs can be changed independently (the AC3/AC4 surface).
_P0 = "Paragraph zero of the operations page. " * 33
_P1 = "Paragraph one describing approval flow. " * 33
_P2 = "Paragraph two with escalation details. " * 33
_V1 = "\n\n".join([_P0, _P1, _P2])
_P2_CHANGED = "Paragraph two rewritten after the reorg. " * 33
_V2 = "\n\n".join([_P0, _P1, _P2_CHANGED])


# ═══════════════════════════════════════════════════════════════════════════
# AC1 — updated => stale on the event; excluded from default retrieval until
#        the refresh completes
# ═══════════════════════════════════════════════════════════════════════════


def test_ac1_update_event_marks_stale_and_default_retrieval_excludes(org, source_content):
    _ingest_and_embed(org, "page/1", _V1)
    assert _artifacts_in(_hits(org)) == {"page/1"}  # current evidence

    source_content["page/1"] = _V2
    _change_event(org, "page/1", "updated")

    # Stale on the event itself — no worker involved yet.
    assert all(row["is_stale"] for row in _rows(org, "page/1"))
    # Excluded from default retrieval while stale...
    assert _hits(org) == []
    # ...but not gone: an explicit include_stale caller still sees it, flagged.
    flagged = _hits(org, include_stale=True)
    assert flagged and all(h.is_stale for h in flagged)

    # The refresh completes -> the artifact is current evidence again, with the
    # NEW content.
    run = refresh.refresh_pending_for_org(org)
    assert run.refreshed == 1
    hits = _hits(org)
    assert _artifacts_in(hits) == {"page/1"}
    assert all(not h.is_stale for h in hits)
    assert any("rewritten after the reorg" in h.content for h in hits)


def test_ac1_created_event_for_unindexed_artifact_queues_it(org, source_content):
    # First-seen artifact: nothing indexed yet, but the event still queues it and
    # the refresh worker indexes it from the resolver.
    source_content["page/new"] = _V1
    _change_event(org, "page/new", "created")
    assert refresh_queue.pending_count(org) == 1

    run = refresh.refresh_pending_for_org(org)
    assert run.refreshed == 1
    assert _artifacts_in(_hits(org)) == {"page/new"}


# ═══════════════════════════════════════════════════════════════════════════
# AC2 — deleted => chunks removed immediately, no window, no orphaned queue row
# ═══════════════════════════════════════════════════════════════════════════


def test_ac2_delete_event_removes_chunks_immediately(org, source_content):
    _ingest_and_embed(org, "page/keep", _V1)
    _ingest_and_embed(org, "page/gone", "A page that will be deleted. " * 40)
    assert _artifacts_in(_hits(org)) == {"page/keep", "page/gone"}

    # A pending refresh exists for the artifact when the deletion lands — the
    # deletion must supersede it, not wait behind it.
    _change_event(org, "page/gone", "updated")
    assert refresh_queue.pending_count(org) == 1

    _change_event(org, "page/gone", "deleted")

    # Immediately — no worker ran, no refresh happened.
    assert _rows(org, "page/gone") == []                      # index: gone
    assert _artifacts_in(_hits(org)) == {"page/keep"}         # default: gone
    assert _artifacts_in(_hits(org, include_stale=True)) == {"page/keep"}  # not even as stale
    assert refresh_queue.pending_count(org) == 0              # no orphaned refresh

    # The surviving artifact is untouched.
    assert all(not row["is_stale"] for row in _rows(org, "page/keep"))


# ═══════════════════════════════════════════════════════════════════════════
# AC3 — hash-compare: unchanged chunks are never re-embedded; only changed
#        content goes back to the gateway
# ═══════════════════════════════════════════════════════════════════════════


def test_ac3_unchanged_hashes_skip_reembedding_changed_content_only(org, source_content):
    chunks = _ingest_and_embed(org, "page/1", _V1)
    assert chunks == 3  # one chunk per paragraph (see content design above)

    source_content["page/1"] = _V2  # only paragraph two changed
    _change_event(org, "page/1", "updated")

    _T7_A.embed_calls.clear()
    run = refresh.refresh_pending_for_org(org)

    assert run.refreshed == 1
    assert run.reused_chunks == 2        # P0, P1: hashes unchanged -> carried over
    assert run.reembedded_chunks == 1    # only the changed paragraph
    embedded_texts = [t for call in _T7_A.embed_calls for t in call]
    assert len(embedded_texts) == 1
    assert "rewritten after the reorg" in embedded_texts[0]
    assert _P0.strip() not in embedded_texts  # unchanged text never re-embedded


def test_ac3_noop_update_costs_zero_embedding_calls(org, source_content):
    _ingest_and_embed(org, "page/1", _V1)
    source_content["page/1"] = _V1  # event fires, content did NOT actually change
    _change_event(org, "page/1", "updated")

    _T7_A.embed_calls.clear()
    run = refresh.refresh_pending_for_org(org)

    assert run.refreshed == 1
    assert run.reused_chunks == 3
    assert run.reembedded_chunks == 0
    assert _T7_A.embed_calls == []  # a noisy source that changes nothing costs nothing
    # And the artifact is current again (stale cleared by the swap).
    assert _artifacts_in(_hits(org)) == {"page/1"}


# ═══════════════════════════════════════════════════════════════════════════
# AC4 — atomic replacement: retrieval never sees a mixed old/new chunk set
# ═══════════════════════════════════════════════════════════════════════════


def test_ac4_refresh_replaces_the_chunk_set_wholesale(org, source_content):
    _ingest_and_embed(org, "page/1", _V1)
    before = {row["chunk_id"]: row for row in _rows(org, "page/1")}

    source_content["page/1"] = _V2
    _change_event(org, "page/1", "updated")
    refresh.refresh_pending_for_org(org)

    after = _rows(org, "page/1")
    after_contents = {row["content"] for row in after}

    # Exactly the new version's chunk set — nothing extra, nothing left behind.
    assert len(after) == 3
    assert _P2_CHANGED.strip() in after_contents
    assert _P2.strip() not in after_contents          # old content fully gone
    assert not any(row["is_stale"] for row in after)  # stale cleared with the swap
    # Unchanged chunks keep their identity through the swap; the changed one is new.
    unchanged_ids = {
        cid for cid, row in before.items() if row["content"] in after_contents
    }
    after_ids = {row["chunk_id"] for row in after}
    assert unchanged_ids <= after_ids
    changed_before = {cid for cid in before if cid not in unchanged_ids}
    assert not (changed_before & after_ids)

    # What retrieval serves is that same single-version set.
    contents_served = {h.content for h in _hits(org)}
    assert contents_served == after_contents


# ═══════════════════════════════════════════════════════════════════════════
# AC5 — model generations are never mixed; the backfill converges to the new one
# ═══════════════════════════════════════════════════════════════════════════


def test_ac5_repin_invalidates_old_vectors_and_backfill_reembeds(org, monkeypatch):
    _ingest_and_embed(org, "page/a", _V1)  # embedded under model A

    # Repin to model B. A new artifact embeds under B.
    monkeypatch.setenv("MODEL_EMBEDDING_PROVIDER", _T7_B.name)
    result = ingest_content(
        org,
        [dict(source_system=_SOURCE, source_artifact="page/b",
              content="Fresh page embedded under the new model. " * 40,
              content_type="prose")],
    )
    assert result.artifacts_indexed == 1
    embedder.embed_pending_for_org(org)

    # Retrieval under B sees ONLY model-B vectors — the old generation is
    # invalidated, not mixed in.
    assert _artifacts_in(_hits(org)) == {"page/b"}
    # And under A, only model-A vectors — never a cross-generation comparison.
    monkeypatch.setenv("MODEL_EMBEDDING_PROVIDER", _T7_A.name)
    assert _artifacts_in(_hits(org)) == {"page/a"}

    # The managed backfill re-embeds the old generation under the active model.
    monkeypatch.setenv("MODEL_EMBEDDING_PROVIDER", _T7_B.name)
    backfill = embedder.backfill_stale_model_for_org(org)
    assert backfill.reembedded > 0

    stamps = {
        (row["embedding_model"], row["embedding_model_version"])
        for artifact in ("page/a", "page/b")
        for row in _rows(org, artifact)
    }
    assert stamps == {("t7:model-b", "2")}  # one generation, everywhere
    assert _artifacts_in(_hits(org)) == {"page/a", "page/b"}


# ═══════════════════════════════════════════════════════════════════════════
# AC6 — stale exclusion is on the assembly selection log: 'excluded: stale'
# ═══════════════════════════════════════════════════════════════════════════


def test_ac6_stale_exclusion_is_logged_never_silent(org):
    _ingest_and_embed(org, "page/1", _V1)
    _change_event(org, "page/1", "updated")  # stale, refresh not yet run

    opportunity = {"title": "Approval flow", "description": "escalation details"}
    package = assemble_context(
        opportunity,
        {"entities": [], "relationships": []},
        AssemblyPolicy(),
        evidence_source=retrieval_evidence_source(org),
    )

    # The stale chunks were PROPOSED (so the exclusion is on the record) but not
    # admitted into context.
    assert package.evidence == []
    stale_entries = [
        e for e in package.selection_log
        if e["kind"] == "evidence" and e["reason"] == REASON_STALE
    ]
    assert stale_entries, "stale exclusions must appear on the selection log"
    assert all(e["decision"] == "excluded" for e in stale_entries)

    # It is a POLICY decision, not a hard-coded filter: a policy that opts in
    # admits the same candidates.
    admitted = assemble_context(
        opportunity,
        {"entities": [], "relationships": []},
        AssemblyPolicy(include_stale=True),
        evidence_source=retrieval_evidence_source(org),
    )
    assert admitted.evidence


# ═══════════════════════════════════════════════════════════════════════════
# AC7 — freshness lag is queryable per org over HTTP
# ═══════════════════════════════════════════════════════════════════════════


def test_ac7_freshness_lag_is_queryable_over_http(org, source_content, monkeypatch):
    seed_owner(org, _DEV_TOKEN)
    headers = {"Authorization": f"Bearer {_DEV_TOKEN}", "X-Org-Id": org}

    _ingest_and_embed(org, "page/1", _V1)
    source_content["page/1"] = _V2
    _change_event(org, "page/1", "updated")
    monkeypatch.setenv("MODEL_EMBEDDING_PROVIDER", _T7_B.name)  # repin: backfill lag

    resp = client.get("/api/retrieval/freshness", headers=headers)
    assert resp.status_code == 200
    out = resp.json()
    assert out["org_id"] == org
    assert out["pending_change_events"] == 1     # the queued refresh
    assert out["stale_chunks"] == 3              # the invalidated artifact
    assert out["backfill"]["awaiting_backfill"] == 3  # all vectors on old model
    assert out["backfill"]["progress"] == 0.0
    assert out["backfill"]["complete"] is False

    # The lag drains as the machinery runs: refresh + backfill -> all clear.
    refresh.refresh_pending_for_org(org)
    embedder.backfill_stale_model_for_org(org)
    embedder.embed_pending_for_org(org)
    out2 = client.get("/api/retrieval/freshness", headers=headers).json()
    assert out2["pending_change_events"] == 0
    assert out2["stale_chunks"] == 0
    assert out2["backfill"]["complete"] is True

    # Per org: a different org's lag picture is untouched by all of the above.
    other = f"{org}_o"[:60]
    seed_owner(other, _DEV_TOKEN)
    _cleanup(other)
    try:
        out_other = client.get(
            "/api/retrieval/freshness",
            headers={"Authorization": f"Bearer {_DEV_TOKEN}", "X-Org-Id": other},
        ).json()
        assert out_other["pending_change_events"] == 0
        assert out_other["stale_chunks"] == 0
        assert out_other["chunks_total"] == 0
    finally:
        _cleanup(other)
