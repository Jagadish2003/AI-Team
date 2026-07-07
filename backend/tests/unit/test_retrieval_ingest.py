"""Unit tests for the ``ingest_content`` producer contract (R18-B1 T5).

Covers the T5 acceptance surface (Section 3 producer contract / AC1 / AC7):

* producers hand extracted text + provenance and NOTHING else — dict payloads
  with substrate-owned fields (``embedding``, ``chunk_size``, ``vector``, …) are
  rejected; the artifact shape has no such fields;
* handed-over content is chunked per its content-type policy and indexed with
  full provenance metadata and a content hash (AC1's substrate side);
* ingestion NEVER embeds: every record is written with ``embedding is None`` and
  the module does not touch the model gateway (AC7 — embedding is the async
  pipeline's job);
* re-ingesting an artifact replaces its previous chunks; a rejected re-ingest
  leaves previously indexed content intact;
* per-artifact failures are isolated — one bad artifact never sinks the batch.

The ``store`` layer is monkeypatched with an in-memory fake so these run in the
unit suite with no PostgreSQL/pgvector dependency; the DB-backed end-to-end
coverage lives in the contract suite (``test_retrieval_ingest_contract.py``).
"""
from __future__ import annotations

import ast
import pathlib
from datetime import datetime, timezone

import pytest

from app.retrieval import ingest
from app.retrieval.chunking import MAX_CHUNK_CHARS
from app.retrieval.ingest import ContentArtifact, IngestResult, ingest_content
from database.models.retrieval import RetrievalChunkRecord, compute_content_hash


# ---------------------------------------------------------------------------
# In-memory store fake — same signatures as app.retrieval.store.
# ---------------------------------------------------------------------------


class _FakeStore:
    def __init__(self):
        self.rows: dict[str, RetrievalChunkRecord] = {}  # chunk_id -> record
        self.delete_calls: list[tuple[str, str, str]] = []

    def delete_by_artifact(self, org_id, source_system, source_artifact):
        self.delete_calls.append((org_id, source_system, source_artifact))
        gone = [
            cid
            for cid, r in self.rows.items()
            if (r.org_id, r.source_system, r.source_artifact)
            == (org_id, source_system, source_artifact)
        ]
        for cid in gone:
            del self.rows[cid]
        return len(gone)

    def upsert_chunks(self, records):
        for rec in records:
            self.rows[rec.chunk_id] = rec
        return len(records)

    def for_artifact(self, org_id, source_artifact):
        return sorted(
            (
                r
                for r in self.rows.values()
                if r.org_id == org_id and r.source_artifact == source_artifact
            ),
            key=lambda r: r.chunk_position,
        )


@pytest.fixture
def fake_store(monkeypatch):
    fake = _FakeStore()
    monkeypatch.setattr(ingest, "store", fake)
    return fake


_PROSE = "\n\n".join(
    f"Paragraph {i} carrying a fair amount of representative body text." for i in range(8)
)


def _artifact(**overrides) -> ContentArtifact:
    base = dict(
        source_system="confluence",
        source_artifact="page/123",
        content=_PROSE,
        content_type="prose",
        source_timestamp="2026-07-07T10:00:00+00:00",
        provenance={"url": "https://example/page/123", "author": "sam"},
    )
    base.update(overrides)
    return ContentArtifact(**base)


# ---------------------------------------------------------------------------
# Call-shape contract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_org", [None, "", "   "])
def test_blank_org_id_rejected(fake_store, bad_org):
    with pytest.raises(ValueError, match="org_id"):
        ingest_content(bad_org, [_artifact()])


@pytest.mark.parametrize(
    "bad_artifacts",
    [None, "just a string", b"bytes", {"source_system": "git"}, _artifact(), 42],
)
def test_artifacts_must_be_a_sequence(fake_store, bad_artifacts):
    with pytest.raises(ValueError, match="sequence"):
        ingest_content("org_a", bad_artifacts)


def test_empty_batch_is_a_valid_no_op(fake_store):
    result = ingest_content("org_a", [])
    assert isinstance(result, IngestResult)
    assert result.artifacts_received == 0
    assert result.chunks_indexed == 0
    assert fake_store.rows == {}


def test_dict_and_dataclass_artifacts_both_accepted(fake_store):
    as_dict = dict(
        source_system="git",
        source_artifact="repo/src/main.py",
        content="def main():\n    return 1\n",
        content_type="code",
    )
    result = ingest_content("org_a", [_artifact(), as_dict])
    assert result.artifacts_indexed == 2
    assert result.artifacts_failed == 0
    systems = {r.source_system for r in fake_store.rows.values()}
    assert systems == {"confluence", "git"}


# ---------------------------------------------------------------------------
# Producers never chunk, embed, or write vectors
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "smuggled",
    [
        {"embedding": [0.1, 0.2]},
        {"vector": [0.1]},
        {"chunk_size": 100},
        {"embedding_model": "rogue-model"},
        {"chunks": ["pre-chunked"]},
    ],
)
def test_substrate_owned_fields_rejected(fake_store, smuggled):
    payload = dict(
        source_system="slack",
        source_artifact="thread/9",
        content="hello there",
        content_type="conversation",
        **smuggled,
    )
    result = ingest_content("org_a", [payload])
    assert result.artifacts_failed == 1
    assert result.artifacts_indexed == 0
    failure = result.artifacts[0]
    assert failure.status == "failed"
    assert "retrieval substrate" in (failure.error or "")
    assert fake_store.rows == {}  # nothing written for a rejected handover


def test_ingest_never_writes_a_vector_or_model_stamp(fake_store):
    ingest_content("org_a", [_artifact()])
    assert fake_store.rows
    for rec in fake_store.rows.values():
        assert rec.embedding is None
        assert rec.embedding_model is None
        assert rec.embedding_model_version is None
        assert rec.embedded_at is None


def test_ingest_module_never_touches_the_model_gateway():
    # AC7/AC2 by construction: ingestion is fully synchronous-model-free. The
    # module must not import the gateway or the embedder — embedding happens
    # only in the async pipeline (T3).
    source = pathlib.Path(ingest.__file__).read_text(encoding="utf-8")
    imported = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
            imported.update(f"{node.module}.{a.name}" for a in node.names)
    assert not any("model_gateway" in name for name in imported), imported
    assert not any("embedder" in name for name in imported), imported


# ---------------------------------------------------------------------------
# AC1 (substrate side): chunked per policy, full provenance + content hash
# ---------------------------------------------------------------------------


def test_content_chunked_per_policy_with_provenance_and_hash(fake_store):
    long_prose = "\n\n".join(
        f"Paragraph {i} with plenty of representative words to force multiple chunks."
        for i in range(120)
    )
    assert len(long_prose) > MAX_CHUNK_CHARS  # must exercise the splitter

    result = ingest_content("org_a", [_artifact(content=long_prose)])
    assert result.artifacts_indexed == 1

    recs = fake_store.for_artifact("org_a", "page/123")
    assert len(recs) > 1  # split, not stored whole
    assert result.chunks_indexed == len(recs)
    for pos, rec in enumerate(recs):
        assert len(rec.content) <= MAX_CHUNK_CHARS
        assert rec.chunk_position == pos
        assert rec.org_id == "org_a"
        assert rec.source_system == "confluence"
        assert rec.source_artifact == "page/123"
        assert rec.content_type == "prose"
        assert rec.provenance == {"url": "https://example/page/123", "author": "sam"}
        assert rec.content_hash == compute_content_hash(rec.content)
        assert rec.source_timestamp == datetime(2026, 7, 7, 10, 0, tzinfo=timezone.utc)


def test_conversation_content_uses_conversation_policy(fake_store):
    convo = "\n\n".join(f"[10:0{i}] user{i}: message number {i}" for i in range(6))
    ingest_content(
        "org_a",
        [
            _artifact(
                source_system="slack",
                source_artifact="thread/42",
                content=convo,
                content_type="conversation",
            )
        ],
    )
    recs = fake_store.for_artifact("org_a", "thread/42")
    assert recs and all(r.content_type == "conversation" for r in recs)


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("2026-07-07T10:00:00Z", datetime(2026, 7, 7, 10, 0, tzinfo=timezone.utc)),
        (datetime(2026, 1, 2, 3, 4, 5), datetime(2026, 1, 2, 3, 4, 5)),
        (None, None),
        ("not-a-timestamp", None),  # bad metadata never blocks the content
    ],
)
def test_source_timestamp_forms(fake_store, raw, expected):
    result = ingest_content("org_a", [_artifact(source_timestamp=raw)])
    assert result.artifacts_indexed == 1
    recs = fake_store.for_artifact("org_a", "page/123")
    assert recs and all(r.source_timestamp == expected for r in recs)


# ---------------------------------------------------------------------------
# Re-ingest: replace semantics, and rejection never destroys indexed content
# ---------------------------------------------------------------------------


def test_reingest_replaces_previous_chunks(fake_store):
    first = ingest_content("org_a", [_artifact()])
    old_ids = set(fake_store.rows)
    assert first.chunks_replaced == 0

    second = ingest_content("org_a", [_artifact(content="One short revision.")])
    assert second.chunks_replaced == len(old_ids)
    assert second.artifacts[0].status == "indexed"
    assert not (old_ids & set(fake_store.rows))  # old chunks are gone
    recs = fake_store.for_artifact("org_a", "page/123")
    assert [r.content for r in recs] == ["One short revision."]


def test_empty_content_clears_previous_chunks(fake_store):
    ingest_content("org_a", [_artifact()])
    assert fake_store.rows

    result = ingest_content("org_a", [_artifact(content="")])
    outcome = result.artifacts[0]
    assert outcome.status == "empty"
    assert outcome.chunks_indexed == 0
    assert outcome.chunks_replaced > 0
    assert fake_store.rows == {}


def test_rejected_reingest_leaves_previous_chunks_intact(fake_store):
    ingest_content("org_a", [_artifact()])
    before = dict(fake_store.rows)

    bad = dict(
        source_system="confluence",
        source_artifact="page/123",
        content="new text",
        content_type="spreadsheet",  # unknown policy -> rejected
    )
    result = ingest_content("org_a", [bad])
    assert result.artifacts_failed == 1
    assert fake_store.rows == before  # validation ran BEFORE any delete
    assert fake_store.delete_calls == [("org_a", "confluence", "page/123")]  # only the first ingest


# ---------------------------------------------------------------------------
# Batch behaviour: isolation + accounting
# ---------------------------------------------------------------------------


def test_one_bad_artifact_never_sinks_the_batch(fake_store):
    good = _artifact(source_artifact="page/good")
    bad = dict(source_system="git", source_artifact="repo/x", content="txt")  # no content_type
    also_good = dict(
        source_system="document",
        source_artifact="doc/7",
        content="Extracted PDF text body.",
        content_type="prose",
    )
    result = ingest_content("org_a", [good, bad, also_good])

    assert result.artifacts_received == 3
    assert result.artifacts_indexed == 2
    assert result.artifacts_failed == 1
    statuses = [a.status for a in result.artifacts]
    assert statuses == ["indexed", "failed", "indexed"]
    failed = result.artifacts[1]
    assert failed.source_system == "git"  # best-effort identifiers on failures
    assert failed.source_artifact == "repo/x"
    assert fake_store.for_artifact("org_a", "page/good")
    assert fake_store.for_artifact("org_a", "doc/7")


def test_store_error_is_isolated_per_artifact(fake_store, monkeypatch):
    calls = {"n": 0}
    real_upsert = fake_store.upsert_chunks

    def flaky_upsert(records):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("db unavailable")
        return real_upsert(records)

    monkeypatch.setattr(fake_store, "upsert_chunks", flaky_upsert)
    result = ingest_content(
        "org_a",
        [_artifact(source_artifact="page/1"), _artifact(source_artifact="page/2")],
    )
    assert result.artifacts_failed == 1
    assert result.artifacts_indexed == 1
    assert not fake_store.for_artifact("org_a", "page/1")
    assert fake_store.for_artifact("org_a", "page/2")


def test_duplicate_artifact_in_one_call_last_wins(fake_store):
    result = ingest_content(
        "org_a",
        [_artifact(content="version one text."), _artifact(content="version two text.")],
    )
    assert result.artifacts_indexed == 2
    recs = fake_store.for_artifact("org_a", "page/123")
    assert [r.content for r in recs] == ["version two text."]
