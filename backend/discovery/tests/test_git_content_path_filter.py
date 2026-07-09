"""
R18-A2 / AT-530 (T2) — contract tests for per-repo include/exclude path filtering.

Covers the acceptance criteria assigned to this subtask:

  AC4 — Excluded paths (vendored/generated/lockfiles) are not ingested; per-repo
        include/exclude is honoured, with sensible defaults editable per org.
  AC7 — Binary files remain skipped-with-reason and are never indexed (the path
        filter complements, and does not replace, the binary skip from T1).

Runs offline against the deterministic ``git_content_sample.json`` fixture (whose
``web-app`` tree seeds vendored/generated/lockfile paths) and with directly-built
``GitRepoConfig`` objects + a fake reader so per-repo/org overrides are exercised
without a live clone or a database.
"""
from __future__ import annotations

import pytest

from discovery.ingest import change_runner
from discovery.ingest.base import ChangeKind, Checkpoint
from discovery.ingest.git_content import (
    DEFAULT_EXCLUDE_GLOBS,
    GitContentIngestor,
    GitRepoConfig,
    PathFilter,
    _as_str_tuple,
    _encode_checkpoint,
    _match_path,
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


class FakeReader:
    """Minimal _RepoReader stand-in for direct _tree_items / _diff_items tests."""

    def __init__(self, tree=None, diff=None, ts="2026-01-01T00:00:00+00:00"):
        self._tree = tree or []
        self._diff = diff or ([], 0)
        self._ts = ts

    def head_sha(self):
        return "sha"

    def head_ts(self):
        return self._ts

    def tree(self):
        return self._tree

    def diff(self, since_sha, head_sha):
        return self._diff


def _repo(repo_id="r", **kw) -> GitRepoConfig:
    return GitRepoConfig(repo_id=repo_id, **kw)


# ─────────────────────────────────────────────────────────────────────────────
# _match_path — gitignore-flavoured glob matching
# ─────────────────────────────────────────────────────────────────────────────
def test_match_path_no_slash_matches_any_segment_at_any_depth():
    assert _match_path("node_modules/left-pad/index.js", "node_modules")
    assert _match_path("frontend/node_modules/x.js", "node_modules")  # nested
    assert _match_path("src/app.min.js", "*.min.js")  # basename glob
    assert _match_path("a/b/yarn.lock", "yarn.lock")  # lockfile anywhere
    assert not _match_path("src/app.py", "node_modules")
    assert not _match_path("src/mainjs", "*.min.js")


def test_match_path_with_slash_matches_full_path_or_subtree():
    assert _match_path("src/generated/api.py", "src/generated")  # dir prefix subtree
    assert _match_path("src/generated/deep/x.py", "src/generated")
    assert _match_path("src/generated/api.py", "src/generated/*")  # glob
    assert not _match_path("src/handwritten/api.py", "src/generated")
    assert not _match_path("other/generated/x.py", "src/generated")  # anchored


def test_match_path_ignores_blank_and_slash_only_patterns():
    assert not _match_path("src/app.py", "")
    assert not _match_path("src/app.py", "/")


# ─────────────────────────────────────────────────────────────────────────────
# PathFilter — include allow-list + exclude semantics
# ─────────────────────────────────────────────────────────────────────────────
def test_pathfilter_empty_admits_everything():
    f = PathFilter()
    assert f.allows("src/app.py")
    assert f.allows("anything/at/all.txt")
    assert not f.allows("")  # a blank path is never in scope


def test_pathfilter_exclude_removes_matches():
    f = PathFilter(exclude=("node_modules", "*.lock"))
    assert f.allows("src/app.py")
    assert not f.allows("node_modules/x.js")
    assert not f.allows("deps.lock")


def test_pathfilter_include_is_an_allow_list():
    f = PathFilter(include=("src/*", "docs/*"))
    assert f.allows("src/app.py")
    assert f.allows("docs/readme.md")
    assert not f.allows("scripts/build.sh")  # not in the allow-list


def test_pathfilter_exclude_wins_within_the_allow_list():
    f = PathFilter(include=("src/*",), exclude=("*.min.js",))
    assert f.allows("src/app.py")
    assert not f.allows("src/bundle.min.js")  # allowed by include, removed by exclude


# ─────────────────────────────────────────────────────────────────────────────
# DEFAULT_EXCLUDE_GLOBS — vendored / generated / lockfiles
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "path",
    [
        "node_modules/left-pad/index.js",  # vendored
        "vendor/github.com/pkg/errors.go",  # vendored
        ".venv/lib/site.py",  # vendored
        "dist/app.min.js",  # generated + minified
        "build/output.o",  # build output
        "target/classes/App.class",  # build output
        "src/orders_pb2.py",  # generated protobuf
        "coverage/lcov.info",  # generated
        "yarn.lock",  # lockfile
        "package-lock.json",  # lockfile
        "poetry.lock",  # lockfile
        "go.sum",  # lockfile
    ],
)
def test_default_excludes_cover_vendored_generated_lockfiles(path):
    ing = GitContentIngestor()
    assert ing._select_paths(_repo(), path) is False


def test_default_excludes_admit_ordinary_source():
    ing = GitContentIngestor()
    for path in ("src/main.py", "README.md", "app/handlers/orders.go", "lib/util.ts"):
        assert ing._select_paths(_repo(), path) is True


# ─────────────────────────────────────────────────────────────────────────────
# AC4 — end-to-end: excluded paths are never ingested on a first run
# ─────────────────────────────────────────────────────────────────────────────
_EXCLUDED_IN_FIXTURE = [
    "web-app:node_modules/left-pad/index.js",
    "web-app:dist/app.min.js",
    "web-app:src/orders_pb2.py",
    "web-app:yarn.lock",
]


def test_ac4_first_run_excludes_vendored_generated_lockfiles_end_to_end():
    store = Store()
    fake = FakeIngest()
    ing = GitContentIngestor(ingest_fn=fake)
    seen: list = []
    res = change_runner.ingest_with_checkpoint(
        ing,
        "org1",
        read_checkpoint=store.read,
        save_checkpoint=store.save,
        process_batch=lambda b: seen.extend(r["artifact_id"] for r in b.records),
    )
    assert res.ok
    # None of the seeded vendored/generated/lockfile paths reached records...
    for excluded in _EXCLUDED_IN_FIXTURE:
        assert excluded not in seen
    # ...nor the retrieval substrate.
    handed = {a.source_artifact for a in fake.artifacts}
    for excluded in _EXCLUDED_IN_FIXTURE:
        assert excluded not in handed
    # Real source under src/ and the README are still ingested.
    assert "web-app:src/main.py" in seen
    assert "web-app:README.md" in seen


def test_ac7_binary_still_skipped_alongside_path_filter():
    """The path filter complements binary skip — the binary asset is still out."""
    store = Store()
    ing = GitContentIngestor(ingest_fn=FakeIngest())
    seen: list = []
    change_runner.ingest_with_checkpoint(
        ing,
        "org1",
        read_checkpoint=store.read,
        save_checkpoint=store.save,
        process_batch=lambda b: seen.extend(r["artifact_id"] for r in b.records),
    )
    assert "web-app:assets/logo.png" not in seen


# ─────────────────────────────────────────────────────────────────────────────
# AC4 — per-repo overrides
# ─────────────────────────────────────────────────────────────────────────────
def test_per_repo_explicit_exclude_is_honoured():
    ing = GitContentIngestor()
    repo = _repo("web-app", exclude=("docs/*", "*.sql"))
    assert ing._select_paths(repo, "docs/guide.md") is False
    assert ing._select_paths(repo, "etl/load.sql") is False
    assert ing._select_paths(repo, "src/app.py") is True  # defaults still admit


def test_per_repo_include_allow_list_narrows_scope():
    ing = GitContentIngestor()
    repo = _repo("svc", include=("src/**",))
    assert ing._select_paths(repo, "src/api/routes.py") is True
    assert ing._select_paths(repo, "scripts/deploy.sh") is False  # outside allow-list


def test_use_default_excludes_false_opts_out_of_builtins():
    ing = GitContentIngestor()
    repo = _repo("svc", use_default_excludes=False)
    # With built-ins disabled, a lockfile / vendored path is admitted again.
    assert ing._select_paths(repo, "yarn.lock") is True
    assert ing._select_paths(repo, "node_modules/x.js") is True


def test_use_default_excludes_false_still_honours_explicit_exclude():
    ing = GitContentIngestor()
    repo = _repo("svc", use_default_excludes=False, exclude=("node_modules",))
    assert ing._select_paths(repo, "node_modules/x.js") is False
    assert ing._select_paths(repo, "yarn.lock") is True  # not excluded now


def test_filter_is_cached_per_repo():
    ing = GitContentIngestor()
    repo = _repo("svc", exclude=("docs/*",))
    first = ing._filter_for(repo)
    assert ing._filter_for(repo) is first  # memoised


# ─────────────────────────────────────────────────────────────────────────────
# AC4 — filter applies to BOTH the tree (first load) and the diff (incremental)
# ─────────────────────────────────────────────────────────────────────────────
def test_tree_items_filter_excluded_and_binary_paths():
    ing = GitContentIngestor()
    reader = FakeReader(
        tree=[
            {"path": "src/app.py", "content": "x = 1\n"},
            {"path": "node_modules/dep/index.js", "content": "1"},
            {"path": "logo.png", "binary": True, "content": ""},
            {"path": "package-lock.json", "content": "{}"},
        ]
    )
    items = ing._tree_items(reader, _repo("r"))
    assert [i.path for i in items] == ["src/app.py"]


def test_diff_items_filter_excluded_paths_including_deletions():
    ing = GitContentIngestor()
    reader = FakeReader(
        diff=(
            [
                {"path": "src/app.py", "change_kind": ChangeKind.UPDATED, "content": "x = 2\n"},
                {"path": "dist/app.min.js", "change_kind": ChangeKind.CREATED, "content": "1"},
                # An excluded path deleted in a commit is not surfaced either.
                {"path": "node_modules/dep/index.js", "change_kind": ChangeKind.DELETED},
                {"path": "src/legacy.py", "change_kind": ChangeKind.DELETED},
            ],
            3,
        )
    )
    items = ing._diff_items(reader, _repo("r"), "old", "new")
    got = {(i.path, i.change_kind) for i in items}
    assert got == {
        ("src/app.py", ChangeKind.UPDATED),
        ("src/legacy.py", ChangeKind.DELETED),
    }


# ─────────────────────────────────────────────────────────────────────────────
# "editable per org" — org-level path defaults merge into repos
# ─────────────────────────────────────────────────────────────────────────────
def test_org_defaults_merge_into_repos(monkeypatch):
    ing = GitContentIngestor()
    monkeypatch.setattr(
        ing,
        "_fixture",
        lambda: {
            "path_defaults": {
                "exclude": ["docs/*"],
                "use_default_excludes": False,
            },
            "repos": [
                {"repo_id": "a", "exclude": ["*.tmp"]},
                {"repo_id": "b", "use_default_excludes": True, "include": ["src/*"]},
            ],
        },
    )
    repos = {r.repo_id: r for r in ing._configured_repos("org1")}

    # Repo 'a' inherits the org default (defaults OFF) and unions the org + repo
    # excludes; a lockfile is now admitted (built-ins disabled) but docs/*.tmp out.
    assert repos["a"].use_default_excludes is False
    assert set(repos["a"].exclude) == {"docs/*", "*.tmp"}
    assert ing._select_paths(repos["a"], "yarn.lock") is True
    assert ing._select_paths(repos["a"], "docs/x.md") is False
    assert ing._select_paths(repos["a"], "notes.tmp") is False

    # Repo 'b' overrides the org default back ON and sets its own include allow-list.
    assert repos["b"].use_default_excludes is True
    assert repos["b"].include == ("src/*",)
    assert ing._select_paths(repos["b"], "src/app.py") is True
    assert ing._select_paths(repos["b"], "scripts/x.sh") is False  # allow-list
    assert ing._select_paths(repos["b"], "src/bundle.min.js") is False  # built-in


def test_as_str_tuple_tolerates_shapes():
    assert _as_str_tuple(None) == ()
    assert _as_str_tuple("src/*") == ("src/*",)
    assert _as_str_tuple(["a", "", "  ", "b"]) == ("a", "b")
    assert _as_str_tuple({"nope": 1}) == ()


# ─────────────────────────────────────────────────────────────────────────────
# Regression: the seeded excluded files do not disturb the incremental diff
# ─────────────────────────────────────────────────────────────────────────────
def test_incremental_still_touches_only_changed_in_scope_files():
    since = Checkpoint.create(
        "git_content",
        "org1",
        _encode_checkpoint(
            {
                "web-app": {"sha": "c1c1c1c1", "offset": None},
                "data-pipeline": {"sha": "d2d2d2d2", "offset": None},
            }
        ),
    )
    ing = GitContentIngestor(ingest_fn=FakeIngest())
    batches = list(ing.ingest_changes("org1", since))
    ids = sorted(r["artifact_id"] for b in batches for r in b.records)
    assert ids == sorted(
        ["web-app:src/main.py", "web-app:src/new_feature.py", "web-app:src/legacy.py"]
    )
