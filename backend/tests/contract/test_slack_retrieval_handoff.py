"""
R18-A4 / AT-594 (T1) — Slack deep content → retrieval, end to end (AC1, AC2).

Proves the T1 acceptance criteria against the REAL R18-B1 substrate, through the
one public path a real run uses:

    SlackIngestor.ingest_deep_content(org, records)   # T1 deep-content hand-off
      → retrieval.ingest_content(org, …)              # B1 T5 producer contract
        → embedder.embed_pending_for_org()            # B1 T3 async, gateway-only
          → retrieve(org, query)                      # B1 T4 source-aware API

  AC1 — conversation content from a selected Slack channel is chunked, indexed,
        and retrievable with thread-level provenance (origin='observed', evidence
        pointer at the exact thread).
  AC2 — content from a private channel is never ingested (seeded and confirmed
        absent from retrieval).

Embedding runs through a FAKE provider registered with the REAL model gateway and
selected via ``MODEL_EMBEDDING_PROVIDER`` (the production path executes; no direct
provider call), exactly as the B1 acceptance suite does. The offline Slack fixture
supplies the channel access list for the scope check (C001/C002 accessible, C900
private).
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
from app.retrieval import embedder
from app.retrieval.api import retrieve
from discovery.ingest.slack import SlackIngestor


# ---------------------------------------------------------------------------
# Skip cleanly where there is no pgvector-backed store (mirrors the B1 suite).
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

_TERMS = ("alpha", "beta", "gamma")


class _SlackFakeProvider(ModelProvider):
    emits_own_telemetry = True

    def __init__(self, name: str, identity):
        self.name = name
        self._identity = identity

    def generate(self, req: GenerationRequest) -> GenerationResult:  # pragma: no cover
        return GenerationResult(text=None, provider=self.name, ok=False)

    def embed(self, texts: List[str]) -> List[List[float]]:
        out = []
        for t in texts:
            low = t.lower()
            out.append([1.0 if term in low else 0.0 for term in _TERMS] + [0.01])
        return out

    def embedding_identity(self):
        return self._identity


_PROVIDER = _SlackFakeProvider("slack_handoff_embed", ("slack-handoff:model", "1"))
register_provider(_PROVIDER)


def _msg(channel_id, ts, user, text, *, thread_ts=None, reply_count=0, channel_name="chan"):
    rec = {
        "channel_id": channel_id,
        "channel_name": channel_name,
        "ts": ts,
        "user": user,
        "text": text,
        "reply_count": reply_count,
        "reply_users_count": 0,
        "reactions": [],
    }
    if thread_ts is not None:
        rec["thread_ts"] = thread_ts
    return rec


def _cleanup(org_id: str) -> None:
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute("DELETE FROM retrieval_chunks WHERE org_id = %s", (org_id,))
        con.commit()
    finally:
        con.close()


def _rows_for(org_id: str, source_artifact: str) -> list:
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT source_system, source_artifact, content_type, provenance "
            "FROM retrieval_chunks WHERE org_id = %s AND source_artifact = %s",
            (org_id, source_artifact),
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        con.close()


@pytest.fixture
def org(request, monkeypatch):
    monkeypatch.setenv("MODEL_EMBEDDING_PROVIDER", _PROVIDER.name)
    monkeypatch.setenv("INGEST_MODE", "offline")  # fixture supplies channel access
    name = f"slack_ho_{request.node.name}"[:60]
    _cleanup(name)
    yield name
    _cleanup(name)


# ===========================================================================
# AC1 — selected-channel conversation delivered and subsequently retrievable
# ===========================================================================
def test_ac1_slack_thread_delivered_and_retrievable(org):
    # A real thread in the accessible channel C001 (parent + reply, thread_ts) and
    # a standalone message in C002 — both channels accessible, no P5 selection.
    records = [
        _msg("C001", "1000.0001", "U1", "the alpha incident on payments", reply_count=1,
             channel_name="ops-incidents"),
        _msg("C001", "1000.0500", "U2", "alpha rollback done", thread_ts="1000.0001",
             channel_name="ops-incidents"),
        _msg("C002", "2000.0001", "U3", "beta deploy to prod", channel_name="deploys"),
    ]
    result = SlackIngestor().ingest_deep_content(org, records)

    assert result.artifacts_failed == 0
    assert result.artifacts_handed_off == 2  # one thread (C001) + one window (C002)
    assert result.artifacts_indexed == 2

    # Chunks indexed under the 'slack' system with thread-level provenance.
    thread_rows = _rows_for(org, "C001:1000.0001")
    assert thread_rows, "no chunks indexed for the C001 thread"
    assert all(r["source_system"] == "slack" for r in thread_rows)
    assert all(r["content_type"] == "conversation" for r in thread_rows)

    # Embedding is async + gateway-driven; once it runs, the distinct term
    # retrieves its OWN thread with thread-level provenance (AC1).
    run = embedder.embed_pending_for_org(org)
    assert run.embedded == result.chunks_indexed

    hits = retrieve(org, "alpha", k=5, source_filter=["slack"])
    assert hits, "alpha did not retrieve the C001 thread"
    top = hits[0]
    assert top.source_system == "slack"
    assert top.source_artifact == "C001:1000.0001"  # thread-level id
    # Author-attributed content survived to retrieval.
    assert "U1:" in top.content and "U2:" in top.content
    # Evidence pointer points at the exact thread, observed.
    ep = top.to_evidence_pointer().to_dict()
    assert ep["source_artifact"] == "C001:1000.0001"
    assert ep["source_system"] == "slack"

    # The other channel's distinct term retrieves its own window, not the thread.
    beta = retrieve(org, "beta", k=5, source_filter=["slack"])
    assert beta and beta[0].source_artifact == "C002:2000.0001"


# ===========================================================================
# AC2 — a private channel's content is never ingested / retrievable
# ===========================================================================
def test_ac2_private_channel_content_never_ingested(org):
    # C900 is private in the fixture; C001 is accessible. Both carry the SAME
    # distinct term so a leak would be unmistakable at retrieval.
    records = [
        _msg("C900", "1.0", "U9", "gamma secret from a private channel"),
        _msg("C001", "1000.0001", "U1", "gamma note in a public channel",
             channel_name="ops-incidents"),
    ]
    result = SlackIngestor().ingest_deep_content(org, records)

    # Only the accessible channel was handed off.
    assert result.artifacts_handed_off == 1
    assert _rows_for(org, "C900:1.0") == []
    assert _rows_for(org, "C001:1000.0001")

    embedder.embed_pending_for_org(org)
    hits = retrieve(org, "gamma", k=10, source_filter=["slack"])
    ids = {h.source_artifact for h in hits}
    assert "C001:1000.0001" in ids
    assert not any(h.source_artifact.startswith("C900") for h in hits)
