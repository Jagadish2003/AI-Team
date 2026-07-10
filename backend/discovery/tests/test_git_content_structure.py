"""
R18-A2 / AT-534 (T6) — structural metadata (directory tree + file inventory).

Covers the subtask (no dedicated AC — "feeds Sprint-2 work"): the Git content
ingestor captures the directory tree and file inventory as lightweight,
graph-facing metadata from the SAME tree walk / commit diff it uses for content,
and persists it WITHOUT embedding it (a separate sink from ``ingest_content``).

Two layers are exercised:
  * the pure builder / delta helpers in ``discovery.ingest.repo_structure``;
  * the ingestor wiring — a first run captures the full HEAD structure, an
    incremental run maintains it from the diff, binaries are inventoried (never
    embedded), excluded paths never enter the structure, and a capture failure
    never sinks content ingestion.

Runs offline against the deterministic fixture and controlled fake readers; the
structural sink / loader are injected so no database is touched.
"""
from __future__ import annotations

import pytest

from discovery.ingest.base import ChangeKind, Checkpoint
from discovery.ingest.git_content import GitContentIngestor, GitRepoConfig, _encode_checkpoint
from discovery.ingest.repo_structure import (
    ROOT_PATH,
    apply_inventory_delta,
    build_repo_structure,
    inventory_from_structure_dict,
    language_for_path,
)


# ─────────────────────────────────────────────────────────────────────────────
# Test doubles
# ─────────────────────────────────────────────────────────────────────────────
class FakeStructureSink:
    """Captures every structural snapshot the ingestor persists (keyed by repo)."""

    def __init__(self):
        self.calls: list = []          # (org_id, structure_dict) in order
        self.by_repo: dict = {}        # repo_id -> latest structure_dict

    def __call__(self, org_id, structure):
        self.calls.append((org_id, structure))
        self.by_repo[structure["repo_id"]] = structure


class FakeStructureLoader:
    """Serves a prior snapshot per repo (seeds the incremental update)."""

    def __init__(self, snapshots=None):
        self.snapshots = snapshots or {}

    def __call__(self, org_id, repo_id):
        return self.snapshots.get(repo_id)


class FakeIngest:
    def __init__(self):
        self.artifacts: list = []

    def __call__(self, org_id, artifacts):
        self.artifacts.extend(artifacts)


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
        return []


# ═════════════════════════════════════════════════════════════════════════════
# Pure builder / helpers
# ═════════════════════════════════════════════════════════════════════════════
def test_language_for_path_best_effort():
    assert language_for_path("src/main.py") == "Python"
    assert language_for_path("app/Handler.java") == "Java"
    assert language_for_path("web/app.tsx") == "TypeScript"
    assert language_for_path("Dockerfile") == "Dockerfile"
    assert language_for_path("assets/logo.png") is None   # not classified
    assert language_for_path(".gitignore") is None        # hidden, no extension


def test_build_structure_derives_files_and_directory_tree():
    structure = build_repo_structure(
        "svc",
        "sha1",
        [("README.md", False), ("src/api/routes.py", False), ("src/main.py", False),
         ("assets/logo.png", True)],
        captured_at="2026-07-09T00:00:00+00:00",
    )
    assert structure.repo_id == "svc"
    assert structure.commit_sha == "sha1"
    assert structure.captured_at == "2026-07-09T00:00:00+00:00"
    assert structure.file_count == 4
    assert structure.binary_file_count == 1

    files = {f.path: f for f in structure.files}
    # A nested source file carries name/dir/extension/language/depth.
    routes = files["src/api/routes.py"]
    assert routes.name == "routes.py"
    assert routes.directory == "src/api"
    assert routes.extension == "py"
    assert routes.language == "Python"
    assert routes.depth == 3
    assert routes.is_binary is False
    # The binary asset IS inventoried — its existence is a structural fact.
    assert files["assets/logo.png"].is_binary is True
    assert files["assets/logo.png"].language is None

    dirs = {d.path: d for d in structure.directories}
    # Single-rooted tree: root + every ancestor directory of every file.
    assert set(dirs) == {ROOT_PATH, "assets", "src", "src/api"}
    assert dirs[ROOT_PATH].parent is None
    assert dirs[ROOT_PATH].depth == 0
    # Root directly holds README.md (1 file) and 2 subdirectories (assets, src).
    assert dirs[ROOT_PATH].file_count == 1
    assert dirs[ROOT_PATH].subdirectory_count == 2
    # src directly holds main.py and the api/ subdirectory.
    assert dirs["src"].parent == ROOT_PATH
    assert dirs["src"].file_count == 1
    assert dirs["src"].subdirectory_count == 1
    assert dirs["src/api"].parent == "src"
    assert dirs["src/api"].file_count == 1


def test_build_structure_is_deterministic_and_dedups_last_wins():
    a = build_repo_structure("r", "s", [("b.py", False), ("a.py", False)])
    b = build_repo_structure("r", "s", [("a.py", False), ("b.py", False)])
    assert a.to_dict() == b.to_dict()  # order-independent
    # Same path twice: the last (binary flag) wins, one inventory entry.
    dup = build_repo_structure("r", "s", [("x.bin", False), ("x.bin", True)])
    assert dup.file_count == 1
    assert dup.files[0].is_binary is True


def test_empty_repo_yields_just_the_root():
    structure = build_repo_structure("r", "s", [])
    assert structure.file_count == 0
    assert [d.path for d in structure.directories] == [ROOT_PATH]


def test_inventory_from_structure_dict_round_trips():
    structure = build_repo_structure("r", "s", [("a.py", False), ("b.png", True)])
    recovered = inventory_from_structure_dict(structure.to_dict())
    assert sorted(recovered) == [("a.py", False), ("b.png", True)]
    # Tolerant of garbage.
    assert inventory_from_structure_dict(None) == []
    assert inventory_from_structure_dict({"files": "nope"}) == []


def test_apply_inventory_delta_upserts_and_deletes():
    prior = [("a.py", False), ("b.py", False), ("sub/c.py", False)]
    out = apply_inventory_delta(
        prior,
        upserts=[("d.py", False), ("b.py", True)],  # add d.py, flip b.py to binary
        deletes=["a.py"],
    )
    assert dict(out) == {"b.py": True, "sub/c.py": False, "d.py": False}


# ═════════════════════════════════════════════════════════════════════════════
# Ingestor — first run captures the full HEAD structure from the tree walk
# ═════════════════════════════════════════════════════════════════════════════
def _ingestor(**kw):
    sink = FakeStructureSink()
    ingest = FakeIngest()
    ing = GitContentIngestor(ingest_fn=ingest, structure_fn=sink, **kw)
    return ing, sink, ingest


def test_first_run_captures_full_structure_per_repo():
    ing, sink, _ = _ingestor()
    list(ing.ingest_changes("org1", None))

    assert set(sink.by_repo) == {"web-app", "data-pipeline"}
    web = sink.by_repo["web-app"]
    assert web["commit_sha"] == "c3c3c3c3"

    paths = {f["path"] for f in web["files"]}
    # In-scope source + the README, plus the binary asset as a structural fact...
    assert {"README.md", "src/api.py", "src/main.py", "src/new_feature.py",
            "src/utils.py", "assets/logo.png"} <= paths
    # ...but vendored/generated/lockfile paths are NOT in the structure (AC4 parity).
    assert "node_modules/left-pad/index.js" not in paths
    assert "dist/app.min.js" not in paths
    assert "src/orders_pb2.py" not in paths
    assert "yarn.lock" not in paths

    # The binary is inventoried but flagged (its body is never embedded).
    logo = next(f for f in web["files"] if f["path"] == "assets/logo.png")
    assert logo["is_binary"] is True
    assert web["binary_file_count"] == 1

    # Directory tree is derived and single-rooted.
    dir_paths = {d["path"] for d in web["directories"]}
    assert {ROOT_PATH, "src", "assets"} <= dir_paths


def test_structure_is_not_embedded():
    """Structure is graph-facing metadata: it goes to the structure sink, never to
    the content substrate. The binary asset proves it — inventoried in structure,
    but never handed to ``ingest_content``."""
    ing, sink, ingest = _ingestor()
    list(ing.ingest_changes("org1", None))

    structure_paths = {f["path"] for f in sink.by_repo["web-app"]["files"]}
    content_artifacts = {a.source_artifact for a in ingest.artifacts}
    assert "assets/logo.png" in structure_paths
    # The binary / excluded files never reached the (embedding) content path.
    assert "web-app:assets/logo.png" not in content_artifacts
    assert "web-app:yarn.lock" not in content_artifacts


def test_unchanged_run_does_not_recapture_structure():
    since = Checkpoint.create(
        "git_content",
        "org1",
        _encode_checkpoint(
            {"web-app": {"sha": "c3c3c3c3", "offset": None},
             "data-pipeline": {"sha": "d2d2d2d2", "offset": None}}
        ),
    )
    ing, sink, _ = _ingestor()
    list(ing.ingest_changes("org1", since))
    assert sink.calls == []  # both repos already at HEAD — nothing to capture


# ═════════════════════════════════════════════════════════════════════════════
# Ingestor — incremental maintenance from the diff (no full re-walk)
# ═════════════════════════════════════════════════════════════════════════════
def _controlled(ing, reader):
    ing._reader = lambda org_id, repo: reader                       # type: ignore
    ing._configured_repos = lambda org_id: [GitRepoConfig(repo_id="r")]  # type: ignore


def _since(sha="old"):
    return Checkpoint.create(
        "git_content", "org1", _encode_checkpoint({"r": {"sha": sha, "offset": None}})
    )


def test_incremental_applies_diff_to_prior_snapshot():
    prior = build_repo_structure(
        "r", "old", [("a.py", False), ("b.py", False), ("sub/c.py", False)]
    ).to_dict()
    sink = FakeStructureSink()
    loader = FakeStructureLoader({"r": prior})
    ing = GitContentIngestor(
        ingest_fn=FakeIngest(), structure_fn=sink, load_structure_fn=loader
    )
    reader = FakeReader(
        sha="new",
        diff=(
            [
                {"path": "d.py", "change_kind": ChangeKind.CREATED, "content": "x=1\n"},
                {"path": "b.py", "change_kind": ChangeKind.DELETED},
            ],
            1,
        ),
    )
    _controlled(ing, reader)

    list(ing.ingest_changes("org1", _since()))

    captured = sink.by_repo["r"]
    assert captured["commit_sha"] == "new"          # advanced to HEAD
    paths = {f["path"] for f in captured["files"]}
    assert paths == {"a.py", "sub/c.py", "d.py"}    # b.py removed, d.py added


def test_incremental_without_prior_snapshot_seeds_from_full_walk():
    """A repo whose content synced before T6 existed has no prior snapshot; the
    diff alone can't describe the whole tree, so structure seeds from a full walk."""
    sink = FakeStructureSink()
    loader = FakeStructureLoader({})  # no prior snapshot for 'r'
    ing = GitContentIngestor(
        ingest_fn=FakeIngest(), structure_fn=sink, load_structure_fn=loader
    )
    reader = FakeReader(
        sha="new",
        tree=[
            {"path": "src/app.py", "content": "x=1\n"},
            {"path": "README.md", "content": "# r\n"},
            {"path": "node_modules/x.js", "content": "1"},  # excluded even when seeding
        ],
        diff=([{"path": "src/app.py", "change_kind": ChangeKind.UPDATED, "content": "x=2\n"}], 1),
    )
    _controlled(ing, reader)

    list(ing.ingest_changes("org1", _since()))

    paths = {f["path"] for f in sink.by_repo["r"]["files"]}
    assert paths == {"src/app.py", "README.md"}  # full tree, excludes honoured


# ═════════════════════════════════════════════════════════════════════════════
# Ingestor — capture is failure-isolated
# ═════════════════════════════════════════════════════════════════════════════
def test_structure_capture_failure_does_not_break_ingestion():
    def boom(org_id, structure):
        raise RuntimeError("structure store unavailable")

    ingest = FakeIngest()
    ing = GitContentIngestor(ingest_fn=ingest, structure_fn=boom)
    # The run still completes and content is still ingested despite the failing sink.
    batches = list(ing.ingest_changes("org1", None))
    assert batches
    assert any(b.records for b in batches)
    assert ingest.artifacts  # file content still handed to the substrate
