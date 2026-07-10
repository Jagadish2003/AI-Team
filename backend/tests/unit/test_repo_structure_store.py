"""
R18-A2 / AT-534 (T6) — unit tests for the graph-facing structural-metadata store.

``app.repo_structure_store`` is the persistence EDGE for repository structure: it
writes/reads the tree+inventory snapshot to the shared KV store, org-scoped and
keyed per repo. These tests exercise the key shape, round-trip, org/repo
isolation, and input validation with the KV layer monkeypatched to an in-memory
dict, so no database is required.
"""
from __future__ import annotations

import pytest

from app import db, repo_structure_store as store


@pytest.fixture
def kv(monkeypatch):
    """Replace the DB-backed KV with an in-memory dict."""
    data: dict = {}
    monkeypatch.setattr(db, "kv_set", lambda key, value: data.__setitem__(key, value))
    monkeypatch.setattr(db, "kv_get", lambda key: data.get(key))
    return data


def _snapshot(repo_id="web-app", sha="c3c3c3c3"):
    return {
        "repo_id": repo_id,
        "commit_sha": sha,
        "captured_at": "2026-07-09T00:00:00+00:00",
        "file_count": 2,
        "directory_count": 2,
        "binary_file_count": 0,
        "files": [{"path": "src/main.py"}, {"path": "README.md"}],
        "directories": [{"path": ""}, {"path": "src"}],
    }


def test_key_is_org_and_repo_scoped():
    assert store.structure_key("acme", "web-app") == "git_content:structure:acme:web-app"
    # Different org / repo never alias.
    assert store.structure_key("acme", "web-app") != store.structure_key("beta", "web-app")
    assert store.structure_key("acme", "web-app") != store.structure_key("acme", "api")


def test_persist_then_load_round_trips(kv):
    snap = _snapshot()
    store.persist_repo_structure("acme", snap)
    assert store.load_repo_structure("acme", "web-app") == snap
    # It landed under the org+repo scoped key.
    assert kv[store.structure_key("acme", "web-app")] == snap


def test_persist_replaces_previous_snapshot(kv):
    store.persist_repo_structure("acme", _snapshot(sha="old"))
    store.persist_repo_structure("acme", _snapshot(sha="new"))
    assert store.load_repo_structure("acme", "web-app")["commit_sha"] == "new"


def test_load_is_org_isolated(kv):
    store.persist_repo_structure("acme", _snapshot())
    # Another org sees nothing for the same repo id.
    assert store.load_repo_structure("beta", "web-app") is None


def test_load_missing_snapshot_is_none(kv):
    assert store.load_repo_structure("acme", "never-seen") is None


def test_persist_rejects_malformed_calls(kv):
    with pytest.raises(ValueError):
        store.persist_repo_structure("", _snapshot())
    with pytest.raises(ValueError):
        store.persist_repo_structure("acme", {"commit_sha": "x"})  # no repo_id
    with pytest.raises(ValueError):
        store.persist_repo_structure("acme", "not-a-dict")  # type: ignore[arg-type]


def test_load_tolerates_blank_ids(kv):
    assert store.load_repo_structure("", "web-app") is None
    assert store.load_repo_structure("acme", "") is None
