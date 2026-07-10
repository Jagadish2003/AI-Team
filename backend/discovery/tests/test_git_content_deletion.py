"""
R18-A2 / AT-533 (T5) — deletion propagation for the Git content ingestor.

Covers the acceptance criterion assigned to this subtask:

  AC3 — A file deleted in a commit emits a ``change_kind='deleted'`` change event
        AND its chunks leave retrieval (freshness integration). Deletion hooks
        directly into the diff mechanism (T1): a file that ``since..HEAD`` removed
        is routed to the substrate's freshness removal so it stops being
        retrievable, in the same batch as the deleted event the runner emits.

Runs offline against the deterministic ``git_content_sample.json`` fixture (whose
``web-app`` c1..c3 diff deletes ``src/legacy.py``) and with a fake reader for
controlled cases. The substrate handover / removal are captured with injected
``ingest_fn`` / ``remove_fn`` so no database is needed; the DB-backed end-to-end
"chunks actually leave the store" coverage lives in the contract suite
(``tests/contract/test_git_content_deletion.py``).
"""
from __future__ import annotations

import pytest

from discovery.ingest import change_runner
from discovery.ingest.base import ChangeKind, Checkpoint
from discovery.ingest.git_content import (
    GitContentIngestor,
    GitRepoConfig,
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
    def __init__(self):
        self.artifacts: list = []

    def __call__(self, org_id, artifacts):
        self.artifacts.extend(artifacts)


class FakeRemove:
    def __init__(self):
        self.calls: list = []       # one entry per _remove_content invocation
        self.removed: list = []     # flattened (source_system, source_artifact)

    def __call__(self, org_id, removals):
        self.calls.append((org_id, list(removals)))
        self.removed.extend(removals)


class FakeReader:
    def __init__(self, tree=None, diff=None, sha="new", ts="2026-01-01T00:00:00+00:00"):
        self._tree = tree or []
        self._diff = diff or ([], 0)
        self._sha = sha
        self._ts = ts

    def head_sha(self):
        return self._sha

    def head_ts(self):
        return self._ts

    def tree(self):
        return self._tree

    def diff(self, since_sha, head_sha):
        return self._diff

    def commits(self, since_sha, head_sha):
        # Complete the _RepoReader contract: ingest_changes reads the commit
        # corpus (AT-532). These deletion cases seed no commits.
        return []


def _ingestor():
    ing = GitContentIngestor()
    ing._ingest_fn = FakeIngest()
    ing._remove_fn = FakeRemove()
    return ing, ing._ingest_fn, ing._remove_fn


def _since(web_sha="c1c1c1c1"):
    return Checkpoint.create(
        "git_content",
        "org1",
        _encode_checkpoint(
            {
                "web-app": {"sha": web_sha, "offset": None},
                "data-pipeline": {"sha": "d2d2d2d2", "offset": None},
            }
        ),
    )


# ─────────────────────────────────────────────────────────────────────────────
# AC3 — the deleted event is still emitted (foundation from T1 preserved)
# ─────────────────────────────────────────────────────────────────────────────
def test_reports_deletes_true():
    assert GitContentIngestor().reports_deletes is True


def test_ac3_deleted_file_emits_deleted_change_event():
    ing, _, _ = _ingestor()
    batches = list(ing.ingest_changes("org1", _since()))
    records = [r for b in batches for r in b.records]
    legacy = next(r for r in records if r["artifact_id"] == "web-app:src/legacy.py")
    assert legacy["change_kind"] == ChangeKind.DELETED
    assert legacy["source_system"] == "git"


def test_ac3_deleted_event_propagates_through_runner():
    """Driven through the real runner, the deletion surfaces as an
    ingestion.artifact_changed event with change_kind='deleted'."""
    events: list = []

    import app.telemetry as telemetry

    orig = telemetry.record_event
    telemetry.record_event = lambda etype, payload=None: events.append((etype, payload or {}))
    try:
        store = Store()
        store.save(_since())
        ing, _, _ = _ingestor()
        change_runner.ingest_with_checkpoint(
            ing, "org1", read_checkpoint=store.read, save_checkpoint=store.save
        )
    finally:
        telemetry.record_event = orig

    deleted = [
        p
        for (e, p) in events
        if e == "ingestion.artifact_changed"
        and p.get("artifact_id") == "web-app:src/legacy.py"
    ]
    assert deleted and deleted[0]["change_kind"] == "deleted"


# ─────────────────────────────────────────────────────────────────────────────
# AC3 — the deleted file's chunks are routed to freshness removal
# ─────────────────────────────────────────────────────────────────────────────
def test_ac3_deleted_file_routed_to_freshness_removal():
    ing, ingest_fn, remove_fn = _ingestor()
    list(ing.ingest_changes("org1", _since()))

    # The deleted file is removed from retrieval...
    assert ("git", "web-app:src/legacy.py") in remove_fn.removed
    # ...and is NOT handed over as content (a deletion carries no body).
    handed = {a.source_artifact for a in ingest_fn.artifacts}
    assert "web-app:src/legacy.py" not in handed
    # The updated / created files ARE ingested but NOT removed.
    assert "web-app:src/main.py" in handed
    assert "web-app:src/new_feature.py" in handed
    assert ("git", "web-app:src/main.py") not in remove_fn.removed


def test_ac3_removal_called_with_correct_org():
    ing, _, remove_fn = _ingestor()
    list(ing.ingest_changes("org-xyz", _since()))
    assert remove_fn.calls
    assert all(org == "org-xyz" for org, _ in remove_fn.calls)


def test_no_removal_when_no_deletions_on_first_run():
    ing, _, remove_fn = _ingestor()
    list(ing.ingest_changes("org1", None))  # first run — all created, none deleted
    assert remove_fn.removed == []


def test_unchanged_run_removes_nothing():
    ing, _, remove_fn = _ingestor()
    since = _since(web_sha="c3c3c3c3")  # already at HEAD
    list(ing.ingest_changes("org1", since))
    assert remove_fn.removed == []


# ─────────────────────────────────────────────────────────────────────────────
# Controlled cases via a fake reader
# ─────────────────────────────────────────────────────────────────────────────
def test_multiple_deletions_all_routed():
    ing = GitContentIngestor()
    remove_fn = FakeRemove()
    ing._ingest_fn = FakeIngest()
    ing._remove_fn = remove_fn
    reader = FakeReader(
        diff=(
            [
                {"path": "a.py", "change_kind": ChangeKind.DELETED},
                {"path": "b.py", "change_kind": ChangeKind.DELETED},
                {"path": "c.py", "change_kind": ChangeKind.UPDATED, "content": "x=1\n"},
            ],
            2,
        )
    )
    ing._reader = lambda org_id, repo: reader  # type: ignore
    ing._configured_repos = lambda org_id: [GitRepoConfig(repo_id="r")]  # type: ignore

    since = Checkpoint.create(
        "git_content", "org1", _encode_checkpoint({"r": {"sha": "old", "offset": None}})
    )
    list(ing.ingest_changes("org1", since))
    assert set(remove_fn.removed) == {("git", "r:a.py"), ("git", "r:b.py")}


def test_excluded_deletion_is_not_routed_to_removal():
    """A deletion of a default-excluded path was never ingested, so it must not be
    routed to removal either — the path filter runs first (AT-530)."""
    ing = GitContentIngestor()
    remove_fn = FakeRemove()
    ing._ingest_fn = FakeIngest()
    ing._remove_fn = remove_fn
    reader = FakeReader(
        diff=(
            [
                {"path": "node_modules/dep/index.js", "change_kind": ChangeKind.DELETED},
                {"path": "src/keep.py", "change_kind": ChangeKind.DELETED},
            ],
            1,
        )
    )
    ing._reader = lambda org_id, repo: reader  # type: ignore
    ing._configured_repos = lambda org_id: [GitRepoConfig(repo_id="r")]  # type: ignore

    since = Checkpoint.create(
        "git_content", "org1", _encode_checkpoint({"r": {"sha": "old", "offset": None}})
    )
    list(ing.ingest_changes("org1", since))
    assert remove_fn.removed == [("git", "r:src/keep.py")]
