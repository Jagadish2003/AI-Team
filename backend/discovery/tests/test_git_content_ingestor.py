"""
R18-A2 / AT-529 (T1) — contract tests for the Git content change-based ingestor.

Covers the acceptance criteria assigned to this subtask:

  AC1 — GitContentIngestor implements ChangeBasedIngestor. A first run streams
        HEAD content as checkpointed, RESUMABLE batches; the checkpoint is the
        head SHA per repo. A failure mid-load resumes from the last fully-
        processed batch rather than restarting, with no skipped/duplicated files.
  AC2 — An incremental run processes ONLY the files the commits since..HEAD
        touched (verified by commit-count vs files-processed); an unchanged
        source returns an empty delta.

Tests run offline (the deterministic ``git_content_sample.json`` fixture) and
drive the ingestor through the REAL runner (``change_runner.ingest_with_checkpoint``)
via an in-memory checkpoint store, so the checkpoint lifecycle is exercised end
to end. Content handover to the retrieval substrate is captured with an injected
``ingest_fn`` so the tests need no database.
"""
from __future__ import annotations

import json

import pytest

from discovery.ingest import change_runner
from discovery.ingest.base import ChangeBasedIngestor, ChangeKind, Checkpoint, DeltaBatch
from discovery.ingest.git_content import (
    FIXTURE_PATH,
    GitContentIngestor,
    _decode_checkpoint,
    _encode_checkpoint,
)


# ─────────────────────────────────────────────────────────────────────────────
# Test doubles
# ─────────────────────────────────────────────────────────────────────────────
class Store:
    def __init__(self):
        self.data: dict = {}

    def read(self, org_id, connector_id):
        return self.data.get((org_id, connector_id))

    def save(self, cp: Checkpoint):
        self.data[(cp.org_id, cp.connector_id)] = cp


class FakeIngest:
    """Captures every ContentArtifact handed to the substrate (no DB needed)."""

    def __init__(self):
        self.artifacts: list = []
        self.calls: int = 0

    def __call__(self, org_id, artifacts):
        self.calls += 1
        self.artifacts.extend(artifacts)


def _ingestor(batch_size: int = 100):
    fake = FakeIngest()
    return GitContentIngestor(batch_size=batch_size, ingest_fn=fake), fake


def _drive(ingestor, org_id, store, **kw):
    return change_runner.ingest_with_checkpoint(
        ingestor, org_id, read_checkpoint=store.read, save_checkpoint=store.save, **kw
    )


def _complete(sha: str) -> dict:
    return {"sha": sha, "offset": None}


# Fixture identities (artifact_id == "{repo_id}:{path}").
_WEB_TREE = [
    "web-app:README.md",
    "web-app:src/api.py",
    "web-app:src/main.py",
    "web-app:src/new_feature.py",
    "web-app:src/utils.py",
]  # assets/logo.png is binary → skipped
_DATA_TREE = ["data-pipeline:etl/extract.py", "data-pipeline:etl/load.sql"]
_ALL_TREE = _WEB_TREE + _DATA_TREE


# ─────────────────────────────────────────────────────────────────────────────
# Contract / shape
# ─────────────────────────────────────────────────────────────────────────────
def test_implements_change_based_ingestor():
    ing, _ = _ingestor()
    assert isinstance(ing, ChangeBasedIngestor)
    assert ing.connector_id == "git_content"
    # git diff natively reports deletions.
    assert ing.reports_deletes is True


def test_records_carry_artifact_id_change_kind_and_source_system():
    ing, _ = _ingestor()
    batches = list(ing.ingest_changes("org1", None))
    records = [r for b in batches for r in b.records]
    assert records
    for r in records:
        assert r["artifact_id"]
        assert r["change_kind"] in ("created", "updated", "deleted")
        assert r["source_system"] == "git"
        assert r["connector_id"] == "git_content"
        assert r["content_type"] == "code"
        # OBSERVED provenance pointer back to the exact file.
        ev = r["evidence_pointer"]
        assert ev["source_system"] == "git"
        assert ev["origin"] == "observed"
        assert ev["source_artifact"] == r["artifact_id"]


# ─────────────────────────────────────────────────────────────────────────────
# AC1 — first run streams HEAD content; checkpoint is the head SHA per repo
# ─────────────────────────────────────────────────────────────────────────────
def test_ac1_first_run_loads_full_head_tree_skipping_binary():
    store = Store()
    ing, fake = _ingestor()
    seen: list = []
    res = _drive(
        ing,
        "org1",
        store,
        process_batch=lambda b: seen.extend(r["artifact_id"] for r in b.records),
    )
    assert res.ok and res.checkpoint_advanced and res.first_run
    assert sorted(seen) == sorted(_ALL_TREE)
    # Binary file is skipped-with-reason, never emitted or indexed.
    assert "web-app:assets/logo.png" not in seen

    # Every non-binary file was handed to the substrate as observed code content.
    # (Commit-message artifacts — content_type 'conversation' — are handed over on
    # the same run but counted separately; AT-532 covers them.)
    file_arts = [a for a in fake.artifacts if a.content_type == "code"]
    assert len(file_arts) == len(_ALL_TREE)
    art = next(a for a in file_arts if a.source_artifact == "web-app:src/main.py")
    assert art.source_system == "git"
    assert art.content_type == "code"
    assert art.content  # real file text
    assert art.provenance["repo"] == "web-app"
    assert art.provenance["path"] == "src/main.py"
    assert art.provenance["commit_sha"] == "c3c3c3c3"
    assert art.provenance["origin"] == "observed"


def test_ac1_checkpoint_is_head_sha_per_repo():
    store = Store()
    ing, _ = _ingestor()
    _drive(ing, "org1", store)
    value = store.read("org1", "git_content").value
    # After a full first load each repo's cursor collapses to a PLAIN head SHA.
    assert json.loads(value)["repos"] == {
        "web-app": "c3c3c3c3",
        "data-pipeline": "d2d2d2d2",
    }
    assert _decode_checkpoint(value) == {
        "web-app": _complete("c3c3c3c3"),
        "data-pipeline": _complete("d2d2d2d2"),
    }


def test_ac1_first_load_streams_multiple_checkpointed_batches():
    store = Store()
    ing, _ = _ingestor(batch_size=1)
    res = _drive(ing, "org1", store)
    assert res.first_run is True
    # 7 non-binary files → 7 single-file batches, each checkpointed (resumable).
    assert res.batches == 7
    assert res.batches_checkpointed == 7
    assert res.complete is True


def test_ac1_failure_midload_resumes_without_loss_or_duplication():
    store = Store()
    processed: list = []

    def fail_on_third(batch: DeltaBatch):
        processed.extend(r["artifact_id"] for r in batch.records)
        if len(processed) == 3:
            raise RuntimeError("network dropped mid initial load")

    ing1, _ = _ingestor(batch_size=1)
    res1 = _drive(ing1, "org1", store, process_batch=fail_on_third)
    assert res1.ok is False
    assert isinstance(res1.error, RuntimeError)
    # Two batches fully processed AND checkpointed; the third raised before its
    # checkpoint was written, so the stored position marks the last good batch.
    assert res1.batches_checkpointed == 2

    # Run 2: a checkpoint now exists → incremental/resume mode. It picks up exactly
    # where it left off — no re-processing of the batches already done.
    resumed: list = []
    ing2, _ = _ingestor(batch_size=1)
    res2 = _drive(
        ing2,
        "org1",
        store,
        process_batch=lambda b: resumed.extend(r["artifact_id"] for r in b.records),
    )
    assert res2.ok and res2.checkpoint_advanced

    already = processed[:2]  # only the two fully-processed batches
    assert not set(already) & set(resumed)
    combined = already + resumed
    assert sorted(combined) == sorted(_ALL_TREE)
    assert len(combined) == len(set(combined)) == len(_ALL_TREE)
    # Load completed across runs → checkpoint at both head SHAs.
    assert _decode_checkpoint(store.read("org1", "git_content").value) == {
        "web-app": _complete("c3c3c3c3"),
        "data-pipeline": _complete("d2d2d2d2"),
    }


# ─────────────────────────────────────────────────────────────────────────────
# AC2 — incremental processes only touched files; unchanged → empty delta
# ─────────────────────────────────────────────────────────────────────────────
def _incremental_since() -> Checkpoint:
    # web-app synced at an OLD commit (c1), data-pipeline already at HEAD (d2).
    return Checkpoint.create(
        "git_content",
        "org1",
        _encode_checkpoint(
            {"web-app": _complete("c1c1c1c1"), "data-pipeline": _complete("d2d2d2d2")}
        ),
    )


def test_ac2_incremental_processes_only_touched_files():
    ing, fake = _ingestor()
    batches = list(ing.ingest_changes("org1", _incremental_since()))
    records = [r for b in batches for r in b.records]
    ids = sorted(r["artifact_id"] for r in records)
    # Only the 3 files the 2 commits (c1..c3) touched — NOT the whole 6-file tree.
    assert ids == sorted(
        ["web-app:src/main.py", "web-app:src/new_feature.py", "web-app:src/legacy.py"]
    )
    # commit-count vs files-processed (AC2): 2 commits touched 3 files, and 3 is
    # strictly fewer than the full HEAD tree — the source was not re-read in full.
    assert len(records) == 3
    assert len(records) < len(_WEB_TREE) + 1  # +1 for the skipped binary in the tree

    by_id = {r["artifact_id"]: r for r in records}
    assert by_id["web-app:src/main.py"]["change_kind"] == "updated"
    assert by_id["web-app:src/new_feature.py"]["change_kind"] == "created"
    assert by_id["web-app:src/legacy.py"]["change_kind"] == "deleted"

    # A deletion carries no content and is not indexed; only the 2 live files are.
    # (Scoped to file content — commit messages, content_type 'conversation', are
    # a separate stream asserted in the AT-532 tests below.)
    handed = {a.source_artifact for a in fake.artifacts if a.content_type == "code"}
    assert handed == {"web-app:src/main.py", "web-app:src/new_feature.py"}


def test_ac2_commit_count_matches_fixture():
    """The fixture records how many commits produced the diff — the other half of
    the commit-count vs files-processed check."""
    with open(FIXTURE_PATH, encoding="utf-8") as fh:
        fixture = json.load(fh)
    web = next(r for r in fixture["repos"] if r["repo_id"] == "web-app")
    diff = web["diffs"]["c1c1c1c1"]
    assert diff["commit_count"] == 2
    assert len(diff["changes"]) == 3  # matches files-processed above


def test_ac2_incremental_advances_only_the_changed_repo():
    store = Store()
    store.save(_incremental_since())
    ing, _ = _ingestor()
    res = _drive(ing, "org1", store)
    assert res.ok and res.checkpoint_advanced
    assert _decode_checkpoint(store.read("org1", "git_content").value) == {
        "web-app": _complete("c3c3c3c3"),      # advanced to HEAD
        "data-pipeline": _complete("d2d2d2d2"),  # untouched
    }


def test_ac2_unchanged_source_returns_empty_delta():
    since = Checkpoint.create(
        "git_content",
        "org1",
        _encode_checkpoint(
            {"web-app": _complete("c3c3c3c3"), "data-pipeline": _complete("d2d2d2d2")}
        ),
    )
    ing, fake = _ingestor()
    batches = list(ing.ingest_changes("org1", since))
    assert len(batches) == 1
    assert batches[0].records == []
    assert batches[0].is_complete is True
    assert fake.artifacts == []  # nothing handed to the substrate
    assert _decode_checkpoint(batches[0].next_checkpoint) == {
        "web-app": _complete("c3c3c3c3"),
        "data-pipeline": _complete("d2d2d2d2"),
    }


def test_ac2_unchanged_run_leaves_checkpoint_untouched():
    store = Store()
    ing1, _ = _ingestor()
    _drive(ing1, "org1", store)  # first run advances to head
    head_value = store.read("org1", "git_content").value

    ing2, _ = _ingestor()
    res = _drive(ing2, "org1", store)  # nothing new
    assert res.ok
    assert res.records == 0
    assert store.read("org1", "git_content").value == head_value


# ─────────────────────────────────────────────────────────────────────────────
# Opaque checkpoint encoding
# ─────────────────────────────────────────────────────────────────────────────
def test_checkpoint_encodes_complete_as_plain_sha_and_inprogress_as_object():
    value = _encode_checkpoint(
        {"a": _complete("sha_a"), "b": {"sha": "sha_b", "offset": 3}}
    )
    assert value == '{"repos":{"a":"sha_a","b":{"offset":3,"sha":"sha_b"}},"v":1}'
    assert _decode_checkpoint(value) == {
        "a": {"sha": "sha_a", "offset": None},
        "b": {"sha": "sha_b", "offset": 3},
    }


def test_decode_checkpoint_is_tolerant_of_garbage():
    assert _decode_checkpoint(None) == {}
    assert _decode_checkpoint("") == {}
    assert _decode_checkpoint("not json") == {}
    assert _decode_checkpoint(json.dumps({"v": 1})) == {}  # no repos key
    assert _decode_checkpoint(json.dumps({"repos": []})) == {}  # wrong type


def test_round_trip_through_runner_then_back_yields_empty_delta():
    store = Store()
    ing1, _ = _ingestor()
    _drive(ing1, "org1", store)
    cp = store.read("org1", "git_content")
    ing2, fake = _ingestor()
    again = list(ing2.ingest_changes("org1", cp))
    assert all(b.is_empty for b in again)
    assert fake.artifacts == []


# ─────────────────────────────────────────────────────────────────────────────
# AC6 — commit-message corpus is retrievable with author/date provenance (AT-532)
# ─────────────────────────────────────────────────────────────────────────────
def _commits(fake) -> list:
    """Commit-message artifacts handed to the substrate (content_type conversation)."""
    return [a for a in fake.artifacts if a.content_type == "conversation"]


def test_ac6_first_run_ingests_full_commit_corpus_with_provenance():
    ing, fake = _ingestor()
    list(ing.ingest_changes("org1", None))

    commits = _commits(fake)
    by_id = {a.source_artifact: a for a in commits}
    # Full corpus at HEAD for both repos: 3 (web-app) + 2 (data-pipeline).
    assert set(by_id) == {
        "web-app@c3c3c3c3",
        "web-app@c2c2c2c2",
        "web-app@c1c1c1c1",
        "data-pipeline@d2d2d2d2",
        "data-pipeline@d1d1d1d1",
    }

    art = by_id["web-app@c3c3c3c3"]
    # Conversation-like content, observed, retrievable text is the commit message.
    assert art.source_system == "git"
    assert art.content_type == "conversation"
    assert "compute_discount" in art.content
    # Author + date provenance attached to EACH message (AC6).
    prov = art.provenance
    assert prov["repo"] == "web-app"
    assert prov["commit_sha"] == "c3c3c3c3"
    assert prov["author"] == "Alice Ng"
    assert prov["author_email"] == "alice@example.com"
    assert prov["date"] == "2026-06-20T14:00:00+00:00"
    assert prov["origin"] == "observed"
    # OBSERVED evidence pointer back to the exact commit.
    ev = prov["evidence_pointer"]
    assert ev["source_system"] == "git"
    assert ev["origin"] == "observed"
    assert ev["source_artifact"] == "web-app@c3c3c3c3"
    assert ev["source_timestamp"] == "2026-06-20T14:00:00+00:00"
    # Timestamp is the commit's authored date, carried to the substrate.
    assert art.source_timestamp == "2026-06-20T14:00:00+00:00"


def test_ac6_commit_artifact_id_namespace_is_disjoint_from_files():
    ing, fake = _ingestor()
    list(ing.ingest_changes("org1", None))
    file_ids = {a.source_artifact for a in fake.artifacts if a.content_type == "code"}
    commit_ids = {a.source_artifact for a in _commits(fake)}
    # A commit message and a file can never collide on (source_system, artifact).
    assert not (file_ids & commit_ids)
    assert all("@" in cid for cid in commit_ids)
    assert all(":" in fid for fid in file_ids)


def test_ac6_commit_messages_are_not_delta_records():
    """Commit messages are CONTENT ONLY — they must not appear as file-change
    delta records, so they never inflate the commit-count-vs-files check (AC2)."""
    ing, _ = _ingestor()
    batches = list(ing.ingest_changes("org1", None))
    records = [r for b in batches for r in b.records]
    # Every delta record is a file (content_type 'code'); no commit slipped in.
    assert records
    assert all(r["content_type"] == "code" for r in records)
    assert all("@" not in r["artifact_id"] for r in records)


def test_ac6_incremental_ingests_only_new_commits():
    ing, fake = _ingestor()
    list(ing.ingest_changes("org1", _incremental_since()))
    commit_ids = {a.source_artifact for a in _commits(fake)}
    # web-app synced at c1 → only the 2 commits c1..c3 introduced (matches the
    # fixture diff's commit_count=2). data-pipeline already at HEAD → no commits.
    assert commit_ids == {"web-app@c3c3c3c3", "web-app@c2c2c2c2"}


def test_ac6_incremental_commit_count_matches_fixture_commit_count():
    """The other half of commit-count vs corpus-size: the number of new commit
    messages equals the matching diff's recorded commit_count."""
    with open(FIXTURE_PATH, encoding="utf-8") as fh:
        fixture = json.load(fh)
    web = next(r for r in fixture["repos"] if r["repo_id"] == "web-app")
    expected = web["diffs"]["c1c1c1c1"]["commit_count"]

    ing, fake = _ingestor()
    list(ing.ingest_changes("org1", _incremental_since()))
    web_commits = [a for a in _commits(fake) if a.provenance["repo"] == "web-app"]
    assert len(web_commits) == expected == 2


def test_ac6_unchanged_source_ingests_no_commits():
    since = Checkpoint.create(
        "git_content",
        "org1",
        _encode_checkpoint(
            {"web-app": _complete("c3c3c3c3"), "data-pipeline": _complete("d2d2d2d2")}
        ),
    )
    ing, fake = _ingestor()
    list(ing.ingest_changes("org1", since))
    assert _commits(fake) == []


def test_ac6_commit_only_repo_ingests_messages_and_advances_checkpoint():
    """A commit that touched no in-scope file still contributes its message and
    still advances the repo to HEAD (commit-only plan)."""
    store = Store()
    # web-app synced at c2 (only c3 is new, and its fixture diff touches no file);
    # data-pipeline already at HEAD.
    store.save(
        Checkpoint.create(
            "git_content",
            "org1",
            _encode_checkpoint(
                {"web-app": _complete("c2c2c2c2"), "data-pipeline": _complete("d2d2d2d2")}
            ),
        )
    )
    ing, fake = _ingestor()
    seen: list = []
    res = _drive(
        ing,
        "org1",
        store,
        process_batch=lambda b: seen.extend(r["artifact_id"] for r in b.records),
    )
    assert res.ok and res.checkpoint_advanced
    # No file records (the one new commit touched nothing in scope) …
    assert seen == []
    # … but the commit message WAS ingested …
    assert {a.source_artifact for a in _commits(fake)} == {"web-app@c3c3c3c3"}
    # … and the repo advanced to HEAD so the message is not re-ingested next run.
    assert _decode_checkpoint(store.read("org1", "git_content").value) == {
        "web-app": _complete("c3c3c3c3"),
        "data-pipeline": _complete("d2d2d2d2"),
    }


def test_ac6_resumed_first_load_does_not_reingest_the_commit_corpus():
    """The corpus is delivered up front on a first load; a resume that has already
    checkpointed part of a repo's tree (offset > 0) must not re-hand its corpus.

    Repos load alphabetically (data-pipeline, then web-app) at batch_size=1. We
    fail on the 4th batch so web-app has ONE checkpointed batch (offset=1) before
    the interruption — a genuine mid-flight resume for web-app on the next run.
    """
    store = Store()
    processed: list = []

    def fail_on_fourth(batch: DeltaBatch):
        processed.extend(r["artifact_id"] for r in batch.records)
        if len(processed) == 4:
            raise RuntimeError("network dropped mid initial load")

    ing1, fake1 = _ingestor(batch_size=1)
    res1 = _drive(ing1, "org1", store, process_batch=fail_on_fourth)
    assert res1.ok is False
    # The interrupted first attempt delivered the whole corpus up front (before any
    # file batch), so both repos' commit messages were already handed over.
    assert {a.provenance["repo"] for a in _commits(fake1)} == {"web-app", "data-pipeline"}
    # web-app is mid-flight: its cursor carries an offset, not a plain SHA.
    web_cursor = _decode_checkpoint(store.read("org1", "git_content").value)["web-app"]
    assert web_cursor["offset"] is not None

    ing2, fake2 = _ingestor(batch_size=1)
    _drive(ing2, "org1", store, process_batch=lambda b: None)
    # web-app's tree load resumes from its offset → its corpus is NOT re-handed.
    assert [a for a in _commits(fake2) if a.provenance["repo"] == "web-app"] == []


def test_ac6_commit_with_empty_message_is_skipped():
    ing = GitContentIngestor()
    arts = ing._commit_artifacts(
        "web-app",
        [
            {"sha": "aaaa", "message": "   ", "author": "X", "date": "2026-01-01T00:00:00+00:00"},
            {"sha": "", "message": "orphan message", "author": "Y"},
            {"sha": "bbbb", "message": "real change", "author": "Z"},
        ],
    )
    # Empty-message and SHA-less commits carry no retrievable 'why' → skipped.
    assert [a.source_artifact for a in arts] == ["web-app@bbbb"]


def test_ac6_commit_message_is_retrievable_through_the_real_chunker():
    """The "retrievable" half of AC6, end to end through the SUBSTRATE chunker:
    a commit artifact's content chunks under the conversation policy and every
    chunk carries the author/date provenance plus a content hash (what retrieval
    and freshness key off). Proven DB-free at the chunk boundary — the store/embed
    stages are the R18-B1 substrate's own tested contract."""
    from app.retrieval import chunking

    ing, _ = _ingestor()
    # Build the real artifact the ingestor would hand over for a HEAD commit.
    art = ing._commit_artifacts(
        "web-app",
        [
            {
                "sha": "c3c3c3c3",
                "message": "Add checkout discount computation\n\nIntroduces "
                "compute_discount() for the checkout flow.",
                "author": "Alice Ng",
                "author_email": "alice@example.com",
                "date": "2026-06-20T14:00:00+00:00",
            }
        ],
    )[0]

    chunks = chunking.chunk_content(
        org_id="org1",
        content=art.content,
        content_type=art.content_type,  # 'conversation'
        source_system=art.source_system,
        source_artifact=art.source_artifact,
        source_timestamp=art.source_timestamp,
        provenance=art.provenance,
    )
    assert chunks, "a commit message must produce at least one retrievable chunk"
    for chunk in chunks:
        assert chunk.content_type == "conversation"
        assert chunk.source_system == "git"
        assert chunk.source_artifact == "web-app@c3c3c3c3"
        # author/date provenance travels with every retrievable chunk (AC6) …
        assert chunk.provenance["author"] == "Alice Ng"
        assert chunk.provenance["date"] == "2026-06-20T14:00:00+00:00"
        assert chunk.source_timestamp == "2026-06-20T14:00:00+00:00"
        # … and a content hash is stamped (freshness / retrieval key off it).
        assert chunk.content_hash


# ─────────────────────────────────────────────────────────────────────────────
# AC5 — secrets redacted BEFORE hand-off; redaction recorded (AT-531)
# ─────────────────────────────────────────────────────────────────────────────
def _artifact(source_artifact, content, content_type="code", repo="web-app"):
    from app.retrieval.ingest import ContentArtifact

    return ContentArtifact(
        source_system="git",
        source_artifact=source_artifact,
        content=content,
        content_type=content_type,
        source_timestamp="2026-06-20T14:00:00+00:00",
        provenance={"repo": repo, "origin": "observed"},
    )


def _capture_events(monkeypatch):
    events: list = []
    monkeypatch.setattr(
        "app.telemetry.record_event", lambda et, p=None: events.append((et, p))
    )
    return events


def test_ac5_secret_scan_redacts_copy_and_records_event(monkeypatch):
    events = _capture_events(monkeypatch)
    ing = GitContentIngestor()
    secret = 'API_KEY = "sk-live-0123456789abcdef"'
    dirty = _artifact("web-app:src/config.py", "x = 1\n" + secret)
    clean = _artifact("web-app:src/ok.py", "def f():\n    return 1\n")

    out = ing._secret_scan("org1", [dirty, clean])

    # Dirty artifact is returned as a REDACTED COPY; the original is not mutated.
    assert "sk-live-0123456789abcdef" not in out[0].content
    assert "[REDACTED:secret_assignment]" in out[0].content
    assert "sk-live-0123456789abcdef" in dirty.content
    assert out[0] is not dirty
    # Clean artifact passes through untouched (same object).
    assert out[1] is clean

    red = [p for et, p in events if et == "ingestion.secret_redacted"]
    assert len(red) == 1
    ev = red[0]
    assert ev["org_id"] == "org1"
    assert ev["connector_id"] == "git_content"
    assert ev["source_artifact"] == "web-app:src/config.py"
    assert ev["redaction_count"] == 1
    assert ev["pattern_types"] == ["secret_assignment"]
    # The event NEVER carries the secret value.
    assert "sk-live-0123456789abcdef" not in json.dumps(ev)


def test_ac5_clean_content_is_not_recorded(monkeypatch):
    events = _capture_events(monkeypatch)
    ing = GitContentIngestor()
    out = ing._secret_scan("org1", [_artifact("web-app:src/ok.py", "print('hi')\n")])
    assert [et for et, _ in events if et == "ingestion.secret_redacted"] == []
    assert out[0].content == "print('hi')\n"


def test_ac5_first_run_redacts_seeded_secrets_before_handoff(monkeypatch):
    events = _capture_events(monkeypatch)
    ing, fake = _ingestor()
    list(ing.ingest_changes("org1", None))

    by_id = {a.source_artifact: a for a in fake.artifacts}
    # Seeded FILE secret (code stream) redacted before the substrate sees it.
    utils = by_id["web-app:src/utils.py"]
    assert "sk-live-0123456789abcdef" not in utils.content
    assert "[REDACTED:secret_assignment]" in utils.content
    # Seeded COMMIT-MESSAGE secret (conversation stream) redacted before hand-off.
    d1 = by_id["data-pipeline@d1d1d1d1"]
    assert "AKIAIOSFODNN7EXAMPLE" not in d1.content
    assert "[REDACTED:aws_access_key_id]" in d1.content

    # No artifact handed to the substrate anywhere contains either seeded secret.
    for a in fake.artifacts:
        assert "sk-live-0123456789abcdef" not in (a.content or "")
        assert "AKIAIOSFODNN7EXAMPLE" not in (a.content or "")

    # Both redactions recorded for run health — pattern types, never the value.
    red = {p["source_artifact"]: p for et, p in events if et == "ingestion.secret_redacted"}
    assert red["web-app:src/utils.py"]["pattern_types"] == ["secret_assignment"]
    assert red["data-pipeline@d1d1d1d1"]["pattern_types"] == ["aws_access_key_id"]
    for p in red.values():
        assert "sk-live-0123456789abcdef" not in json.dumps(p)
        assert "AKIAIOSFODNN7EXAMPLE" not in json.dumps(p)


def test_ac5_scan_runs_for_both_content_streams():
    """Structural guard: the unconditional seam is invoked on BOTH the file batch
    and the commit corpus paths (dep T2 + T4)."""
    import inspect

    src = inspect.getsource(GitContentIngestor.ingest_changes)
    src += inspect.getsource(GitContentIngestor._ingest_commits)
    # Every hand-off to the substrate is wrapped in the secret scan.
    assert src.count("_secret_scan(org_id") == 2
    assert "_ingest_content(org_id, self._secret_scan(org_id" in src


# ─────────────────────────────────────────────────────────────────────────────
# AT-531 review — substrate hand-off failure must NOT advance the checkpoint (#1)
# ─────────────────────────────────────────────────────────────────────────────
def test_review1_substrate_failure_does_not_advance_checkpoint_and_retries():
    """A batch the substrate rejects (artifacts_failed > 0) must not be
    checkpointed past — at-least-once. The next run re-reads and, once the
    substrate accepts it, advances. Mirrors the documents-ingestor contract."""
    from types import SimpleNamespace

    from discovery.ingest.git_content import GitContentHandoffError

    store = Store()

    # Fails the file (code) stream, accepts the commit (conversation) corpus — so
    # the failure is exercised on the first-load file batch specifically.
    def failing_on_code(org_id, artifacts):
        arts = list(artifacts)
        failed = sum(1 for a in arts if getattr(a, "content_type", None) == "code")
        return SimpleNamespace(artifacts_failed=failed, artifacts_indexed=0)

    ing1 = GitContentIngestor(batch_size=1, ingest_fn=failing_on_code)
    res1 = _drive(ing1, "org1", store)
    assert res1.ok is False
    assert isinstance(res1.error, GitContentHandoffError)
    # The failing file batch was never checkpointed → nothing persisted (the run
    # raised before the first file batch could be yielded/advanced).
    assert store.read("org1", "git_content") is None

    # Run 2 with a healthy substrate: the previously-dropped content is re-read
    # (never silently skipped) and now fully indexed, and the checkpoint advances.
    ing2, fake2 = _ingestor(batch_size=1)
    res2 = _drive(ing2, "org1", store)
    assert res2.ok and res2.checkpoint_advanced
    handed = {a.source_artifact for a in fake2.artifacts}
    assert set(_ALL_TREE).issubset(handed)  # every file recovered on retry
    assert _decode_checkpoint(store.read("org1", "git_content").value) == {
        "web-app": _complete("c3c3c3c3"),
        "data-pipeline": _complete("d2d2d2d2"),
    }


def test_review1_partial_batch_failure_still_blocks_advance():
    """Even one failed artifact in an otherwise-successful batch blocks the
    checkpoint (consistent with the documents contract)."""
    from types import SimpleNamespace

    from discovery.ingest.git_content import GitContentHandoffError

    store = Store()
    ing = GitContentIngestor(
        batch_size=100,
        ingest_fn=lambda org, arts: SimpleNamespace(artifacts_failed=1, artifacts_indexed=5),
    )
    res = _drive(ing, "org1", store)
    assert res.ok is False
    assert isinstance(res.error, GitContentHandoffError)
    assert store.read("org1", "git_content") is None


# ─────────────────────────────────────────────────────────────────────────────
# AT-531 review — _match_path: ** matches zero intermediate directories (#4)
# ─────────────────────────────────────────────────────────────────────────────
def test_review4_double_star_matches_zero_intermediate_directories():
    from discovery.ingest.git_content import _match_path

    # The bug: a file directly under the prefix escaped a '**/' exclude.
    assert _match_path("src/foo.py", "src/**/*.py") is True       # zero dirs (fixed)
    assert _match_path("src/a/b/foo.py", "src/**/*.py") is True   # >=1 dir (already)
    assert _match_path("foo.py", "**/*.py") is True               # leading **/
    # Still correctly scoped — a different top-level dir is not matched.
    assert _match_path("other/foo.py", "src/**/*.py") is False


def test_review4_double_star_exclude_drops_file_directly_under_prefix():
    """End-to-end: a per-repo 'src/**/*.py' exclude now drops src/foo.py too."""
    from discovery.ingest.git_content import GitRepoConfig, PathFilter

    f = PathFilter(exclude=("src/**/*.py",))
    assert f.allows("src/foo.py") is False       # the previously-escaping case
    assert f.allows("src/pkg/bar.py") is False
    assert f.allows("docs/readme.md") is True
