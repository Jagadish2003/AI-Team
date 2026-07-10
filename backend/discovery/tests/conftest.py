"""Shared fixtures for the offline discovery test suite.

Discovery tests are deterministic and MUST run without live credentials or a
database (CLAUDE.md, "Testing Guidance"). The R18-A2 Git content ingestor's
structural-metadata capture (AT-534) defaults to persisting through
``app.repo_structure_store`` → ``app.db`` (Postgres), which an offline discovery
run has no business touching. This autouse fixture neutralises that DEFAULT sink
so tests that drive ``ingest_changes`` without opting into structure capture stay
DB-free and fast.

Tests that assert on structure capture inject their own ``structure_fn`` /
``load_structure_fn`` into ``GitContentIngestor`` and therefore never reach the
default patched here — the injection short-circuits the lazy import below.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_db_repo_structure_sink(monkeypatch):
    """Default the graph-facing structure sink to in-memory no-ops (offline)."""
    try:
        import app.repo_structure_store as store
    except Exception:  # pragma: no cover — app not importable in a minimal env
        return
    monkeypatch.setattr(store, "persist_repo_structure", lambda org_id, structure: None)
    monkeypatch.setattr(store, "load_repo_structure", lambda org_id, repo_id: None)
