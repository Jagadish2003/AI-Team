"""
R18-A4 / AT-595 (T2) — Teams deep content → retrieval, end to end (AC1, AC2).

Proves the T2 acceptance criteria against the REAL R18-B1 substrate, through the
one public path a real run uses:

    TeamsIngestor.ingest_deep_content(org, records)   # T2 deep-content hand-off
      → retrieval.ingest_content(org, …)              # B1 T5 producer contract
        → embedder.embed_pending_for_org()            # B1 T3 async, gateway-only
          → retrieve(org, query)                      # B1 T4 source-aware API

  AC1 — conversation content from a granted Teams channel is chunked, indexed,
        and retrievable with thread-level provenance (origin='observed', evidence
        pointer at the exact thread).
  AC2 — content from a private Teams channel is never ingested (seeded and
        confirmed absent from retrieval).

Mirrors ``test_slack_retrieval_handoff.py`` — the depth path is the SAME shared
conversation model; only the Graph collection edge differs. The offline Teams
fixture supplies the granted-channel access list for the scope check
(19:ops/19:deploys granted, 19:leads-private private).
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
from discovery.ingest.teams import TeamsIngestor


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


class _TeamsFakeProvider(ModelProvider):
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


_PROVIDER = _TeamsFakeProvider("teams_handoff_embed", ("teams-handoff:model", "1"))
register_provider(_PROVIDER)


def _rec(
    channel_id,
    message_id,
    user,
    text,
    *,
    team_id="T-eng",
    reply_to_id=None,
    reply_count=0,
    channel_name="chan",
    created="2026-06-10T09:00:00Z",
    display=None,
):
    return {
        "source_system": "teams",
        "team_id": team_id,
        "team_name": "Engineering",
        "channel_id": channel_id,
        "channel_name": channel_name,
        "message_id": message_id,
        "reply_to_id": reply_to_id,
        "reply_count": reply_count,
        "created_at": created,
        "last_modified_at": None,
        "user": user,
        "user_display_name": display or user,
        "text": text,
    }


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
    name = f"teams_ho_{request.node.name}"[:60]
    _cleanup(name)
    yield name
    _cleanup(name)


# ===========================================================================
# AC1 — granted-channel conversation delivered and subsequently retrievable
# ===========================================================================
def test_ac1_teams_thread_delivered_and_retrievable(org):
    # A real thread in granted channel 19:ops (parent + reply via reply_to_id) and
    # a standalone message in granted channel 19:deploys.
    records = [
        _rec("19:ops", "m1", "U100", "the alpha incident on payments", reply_count=1,
             display="Ada", channel_name="ops-incidents", created="2026-06-10T09:00:00Z"),
        _rec("19:ops", "m2", "U101", "alpha rollback done", reply_to_id="m1",
             display="Lin", channel_name="ops-incidents", created="2026-06-10T09:05:00Z"),
        _rec("19:deploys", "d1", "U200", "beta deploy to prod", display="Rae",
             channel_name="deploys", created="2026-06-10T09:10:00Z"),
    ]
    result = TeamsIngestor().ingest_deep_content(org, records)

    assert result.artifacts_failed == 0
    assert result.artifacts_handed_off == 2  # one thread (ops) + one window (deploys)
    assert result.artifacts_indexed == 2

    thread_rows = _rows_for(org, "T-eng/19:ops:m1")
    assert thread_rows, "no chunks indexed for the ops thread"
    assert all(r["source_system"] == "teams" for r in thread_rows)
    assert all(r["content_type"] == "conversation" for r in thread_rows)

    run = embedder.embed_pending_for_org(org)
    assert run.embedded == result.chunks_indexed

    hits = retrieve(org, "alpha", k=5, source_filter=["teams"])
    assert hits, "alpha did not retrieve the ops thread"
    top = hits[0]
    assert top.source_system == "teams"
    assert top.source_artifact == "T-eng/19:ops:m1"  # thread-level id
    # Author-attributed content (display names) survived to retrieval.
    assert "Ada:" in top.content and "Lin:" in top.content
    ep = top.to_evidence_pointer().to_dict()
    assert ep["source_artifact"] == "T-eng/19:ops:m1"
    assert ep["source_system"] == "teams"

    beta = retrieve(org, "beta", k=5, source_filter=["teams"])
    assert beta and beta[0].source_artifact == "T-eng/19:deploys:d1"


# ===========================================================================
# AC2 — a private channel's content is never ingested / retrievable
# ===========================================================================
def test_ac2_private_channel_content_never_ingested(org):
    # 19:leads-private is private in the fixture; 19:ops is granted. Both carry the
    # SAME distinct term so a leak would be unmistakable at retrieval.
    records = [
        _rec("19:leads-private", "p1", "U900", "gamma secret from a private channel",
             display="Exec"),
        _rec("19:ops", "m1", "U100", "gamma note in a granted channel",
             display="Ada", channel_name="ops-incidents"),
    ]
    result = TeamsIngestor().ingest_deep_content(org, records)

    assert result.artifacts_handed_off == 1
    assert _rows_for(org, "T-eng/19:leads-private:p1") == []
    assert _rows_for(org, "T-eng/19:ops:m1")

    embedder.embed_pending_for_org(org)
    hits = retrieve(org, "gamma", k=10, source_filter=["teams"])
    ids = {h.source_artifact for h in hits}
    assert "T-eng/19:ops:m1" in ids
    assert not any("leads-private" in h.source_artifact for h in hits)
