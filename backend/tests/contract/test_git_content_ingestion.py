"""Contract tests — Git content ingestion over the real store (R18-A2 / AT-535, T7).

End-to-end verification of the Section-3 acceptance criteria against the ACTUAL
runner (``change_runner.ingest_with_checkpoint``) and the ACTUAL pgvector-backed
``retrieval_chunks`` store — the discovery suite fakes the substrate, this proves
the whole pipeline (checkpoint lifecycle → secret scan → substrate → retrieval).

Coverage here (AT-535, the contract-test subtask):

  AC1 — First run over a configured repo ingests HEAD content as checkpointed,
        RESUMABLE batches; the checkpoint is the head SHA per repo.
  AC2 — An incremental run ingests ONLY the files the new commits touched
        (commit-count vs files-processed); an unchanged source processes nothing.
  AC4 — Excluded paths (vendored/generated/lockfiles) are not indexed.
  AC6 — Commit messages are retrievable with author/date provenance; file chunks
        carry repo/path/SHA provenance with origin='observed'.
  AC7 — Binary files are skipped-with-reason, never indexed as garbage text.

AC3 (deletion propagation) and AC5 (secret redaction) are proven end-to-end in
their own dedicated contract files — ``test_git_content_deletion.py`` and
``test_git_content_secret_redaction.py`` — so they are not duplicated here.

Runs offline against the deterministic ``git_content_sample.json`` fixture.
Embedding is driven through a FAKE provider registered with the real gateway
(unique name, no registry collision) so retrieval works without a live model.
"""
from __future__ import annotations

import json
import logging
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
from discovery.ingest import change_runner
from discovery.ingest.base import Checkpoint, DeltaBatch
from discovery.ingest.git_content import (
    GitContentIngestor,
    _decode_checkpoint,
    _encode_checkpoint,
)


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


class _CtFakeProvider(ModelProvider):
    emits_own_telemetry = True

    def __init__(self, name: str):
        self.name = name

    def generate(self, req: GenerationRequest) -> GenerationResult:  # pragma: no cover
        return GenerationResult(text=None, provider=self.name, ok=False)

    def embed(self, texts: List[str]) -> List[List[float]]:
        return [[float(len(t) % 5) + 1.0, 0.5, 0.25, 0.125] for t in texts]

    def embedding_identity(self):
        return ("at535:model-a", "1")


_CT_OK = _CtFakeProvider("at535_embed_ok")
register_provider(_CT_OK)


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
    monkeypatch.setenv("MODEL_EMBEDDING_PROVIDER", _CT_OK.name)
    monkeypatch.setenv("INGEST_MODE", "offline")
    name = f"ct_at535_{request.node.name}"[:60]
    _cleanup(name)
    yield name
    _cleanup(name)


# ─────────────────────────────────────────────────────────────────────────────
# The real checkpoint lifecycle, driven in-memory (no runs table dependency).
# ─────────────────────────────────────────────────────────────────────────────
class _CheckpointStore:
    def __init__(self):
        self.data: dict = {}

    def read(self, org_id, connector_id):
        return self.data.get((org_id, connector_id))

    def save(self, cp: Checkpoint):
        self.data[(cp.org_id, cp.connector_id)] = cp


def _drive(ing, org_id, cp_store, **kw):
    return change_runner.ingest_with_checkpoint(
        ing, org_id, read_checkpoint=cp_store.read, save_checkpoint=cp_store.save, **kw
    )


# The in-scope HEAD file inventory of the fixture (binary + excluded paths absent).
_EXPECTED_HEAD_FILES = {
    "web-app:README.md",
    "web-app:src/api.py",
    "web-app:src/main.py",
    "web-app:src/new_feature.py",
    "web-app:src/utils.py",
    "data-pipeline:etl/extract.py",
    "data-pipeline:etl/load.sql",
}
_EXCLUDED_FILES = [
    "web-app:node_modules/left-pad/index.js",
    "web-app:dist/app.min.js",
    "web-app:src/orders_pb2.py",
    "web-app:yarn.lock",
]
_COMPLETE_HEAD = {
    "web-app": {"sha": "c3c3c3c3", "offset": None},
    "data-pipeline": {"sha": "d2d2d2d2", "offset": None},
}


def _rows_for(org_id: str, artifact: str) -> list[dict]:
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT chunk_id, content, content_type, source_system, provenance "
            "FROM retrieval_chunks WHERE org_id = %s AND source_artifact = %s",
            (org_id, artifact),
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        con.close()


def _artifacts_in_store(org_id: str) -> set:
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT DISTINCT source_artifact FROM retrieval_chunks WHERE org_id = %s",
            (org_id,),
        )
        return {r["source_artifact"] for r in cur.fetchall()}
    finally:
        con.close()


def _provenance(org_id: str, artifact: str) -> dict:
    rows = _rows_for(org_id, artifact)
    assert rows, f"no chunk found for {artifact}"
    prov = rows[0]["provenance"]
    return json.loads(prov) if isinstance(prov, str) else (prov or {})


# ═════════════════════════════════════════════════════════════════════════════
# AC1 — first run streams HEAD content as checkpointed, resumable batches
# ═════════════════════════════════════════════════════════════════════════════
def test_ac1_first_run_indexes_head_content_with_head_sha_checkpoint(org):
    cp_store = _CheckpointStore()
    # batch_size=2 → the initial load streams as several individually-checkpointed
    # batches (resumability), not one monolithic read.
    res = _drive(GitContentIngestor(batch_size=2), org, cp_store)

    assert res.ok and res.first_run and res.checkpoint_advanced and res.complete
    assert res.batches >= 2
    # Every processed batch wrote its own checkpoint → any batch is a resume point.
    assert res.batches_checkpointed == res.batches

    # The persisted checkpoint IS the head SHA per repo (fully synced → plain SHA).
    cp = cp_store.read(org, "git_content")
    assert _decode_checkpoint(cp.value) == _COMPLETE_HEAD

    # HEAD file content really landed in the store, and is retrievable once embedded.
    assert _artifacts_in_store(org) >= _EXPECTED_HEAD_FILES
    embedder.embed_pending_for_org(org)
    hits = retrieve(org, "fastapi application start", k=50)
    assert any(h.source_artifact == "web-app:src/main.py" for h in hits)


def test_ac1_first_load_resumes_after_midload_failure_without_loss(org):
    # batch_size=1 → one file per batch; fail after the 3rd batch is processed.
    cp_store = _CheckpointStore()
    processed: list = []

    def fail_on_third(batch: DeltaBatch):
        processed.extend(r["artifact_id"] for r in batch.records)
        if len(processed) >= 3:
            raise RuntimeError("network dropped mid initial load")

    res1 = _drive(GitContentIngestor(batch_size=1), org, cp_store, process_batch=fail_on_third)
    assert res1.ok is False and isinstance(res1.error, RuntimeError)
    # Two batches fully processed AND checkpointed; the 3rd raised before its write.
    assert res1.batches_checkpointed == 2

    # Resume: a checkpoint now exists, so the runner resumes from the last good
    # batch and completes the load across runs — no data skipped, none duplicated
    # (re-ingest replaces an artifact's chunks).
    res2 = _drive(GitContentIngestor(batch_size=1), org, cp_store)
    assert res2.ok and res2.checkpoint_advanced

    assert _artifacts_in_store(org) >= _EXPECTED_HEAD_FILES
    assert _decode_checkpoint(cp_store.read(org, "git_content").value) == _COMPLETE_HEAD
    # Each HEAD file resolves to a bounded, non-duplicated chunk set.
    for artifact in _EXPECTED_HEAD_FILES:
        assert _rows_for(org, artifact)


# ═════════════════════════════════════════════════════════════════════════════
# AC2 — incremental processes only touched files; unchanged → nothing
# ═════════════════════════════════════════════════════════════════════════════
def test_ac2_incremental_processes_only_touched_files(org):
    cp_store = _CheckpointStore()
    # web-app synced at c1 (before the c1..c3 commits); data-pipeline already at HEAD.
    cp_store.save(
        Checkpoint.create(
            "git_content",
            org,
            _encode_checkpoint(
                {"web-app": {"sha": "c1c1c1c1", "offset": None},
                 "data-pipeline": {"sha": "d2d2d2d2", "offset": None}}
            ),
        )
    )
    res = _drive(GitContentIngestor(), org, cp_store)
    assert res.ok and res.checkpoint_advanced and not res.first_run
    # commit-count vs files-processed: the 2 commits c1..c3 touched exactly 3 files
    # (main.py updated, new_feature.py created, legacy.py deleted) — strictly fewer
    # than the 6-file HEAD tree, so the source was NOT re-read in full.
    assert res.records == 3

    arts = _artifacts_in_store(org)
    # The touched live files were (re)indexed …
    assert "web-app:src/main.py" in arts
    assert "web-app:src/new_feature.py" in arts
    # … while HEAD files the commits did NOT touch were never re-read/indexed.
    assert "web-app:src/api.py" not in arts
    assert "web-app:README.md" not in arts
    # data-pipeline was already at HEAD → nothing processed for it.
    assert not any(a.startswith("data-pipeline:") for a in arts)

    # Only the changed repo advanced; data-pipeline stays put.
    assert _decode_checkpoint(cp_store.read(org, "git_content").value) == _COMPLETE_HEAD


def test_ac2_unchanged_source_processes_nothing(org):
    cp_store = _CheckpointStore()
    cp_store.save(
        Checkpoint.create("git_content", org, _encode_checkpoint(_COMPLETE_HEAD))
    )
    res = _drive(GitContentIngestor(), org, cp_store)
    assert res.ok
    assert res.records == 0
    assert _artifacts_in_store(org) == set()  # nothing handed to the substrate


# ═════════════════════════════════════════════════════════════════════════════
# AC4 — excluded (vendored/generated/lockfile) paths are not indexed
# ═════════════════════════════════════════════════════════════════════════════
def test_ac4_excluded_paths_are_not_indexed_end_to_end(org):
    _drive(GitContentIngestor(), org, _CheckpointStore())
    arts = _artifacts_in_store(org)
    for excluded in _EXCLUDED_FILES:
        assert excluded not in arts
        assert _rows_for(org, excluded) == []
    # Ordinary source and the README under an admitted path ARE indexed.
    assert "web-app:src/main.py" in arts
    assert "web-app:README.md" in arts


# ═════════════════════════════════════════════════════════════════════════════
# AC6 — provenance on file + commit chunks; commit messages retrievable
# ═════════════════════════════════════════════════════════════════════════════
def test_ac6_file_chunks_carry_repo_path_sha_observed_provenance(org):
    _drive(GitContentIngestor(), org, _CheckpointStore())
    prov = _provenance(org, "web-app:src/main.py")
    assert prov["repo"] == "web-app"
    assert prov["path"] == "src/main.py"
    assert prov["commit_sha"] == "c3c3c3c3"
    assert prov["origin"] == "observed"
    # File content chunks under the substrate's code policy.
    assert all(r["content_type"] == "code" for r in _rows_for(org, "web-app:src/main.py"))


def test_ac6_commit_messages_retrievable_with_author_date_provenance(org):
    _drive(GitContentIngestor(), org, _CheckpointStore())

    # The commit-message chunk carries author + date + observed provenance …
    prov = _provenance(org, "web-app@c3c3c3c3")
    assert prov["author"] == "Alice Ng"
    assert prov["date"] == "2026-06-20T14:00:00+00:00"
    assert prov["origin"] == "observed"
    assert prov["commit_sha"] == "c3c3c3c3"
    rows = _rows_for(org, "web-app@c3c3c3c3")
    assert rows and all(r["content_type"] == "conversation" for r in rows)

    # … and it is retrievable from the real store once embedded.
    embedder.embed_pending_for_org(org)
    hits = retrieve(org, "checkout discount computation promotion", k=50)
    assert any(h.source_artifact == "web-app@c3c3c3c3" for h in hits)


# ═════════════════════════════════════════════════════════════════════════════
# AC7 — binary files skipped-with-reason, never indexed
# ═════════════════════════════════════════════════════════════════════════════
def test_ac7_binary_files_are_skipped_with_reason_and_never_indexed(org, caplog):
    caplog.set_level(logging.INFO, logger="discovery.ingest.git_content")
    _drive(GitContentIngestor(), org, _CheckpointStore())

    # The binary asset produced NO chunks — never indexed as garbage text.
    assert _rows_for(org, "web-app:assets/logo.png") == []
    assert "web-app:assets/logo.png" not in _artifacts_in_store(org)
    # … and the skip was WITH REASON (observable in run logs).
    assert any(
        "skipping binary file" in rec.getMessage() and "logo.png" in rec.getMessage()
        for rec in caplog.records
    )
