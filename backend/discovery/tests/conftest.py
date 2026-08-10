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
def _offline_ingest_mode(monkeypatch):
    """Pin ``INGEST_MODE=offline`` for every discovery test.

    Discovery tests must be deterministic and credential-free, but ``INGEST_MODE``
    was effectively decided by IMPORT ORDER across the whole session:

    * ``backend/.env`` sets ``INGEST_MODE="live"`` for local dev.
    * Several app modules — ``app/db.py``, ``app/main.py``, ``app/auth/configs.py``,
      ``app/llm_enrichment.py``, ``app/models_t2.py`` and ``discovery/runner.py`` —
      call ``load_dotenv()`` at import time. Collecting any test that imports one
      (directly or transitively) therefore loads that ``live`` into the process.
    * Several test modules compensate with a module-level
      ``os.environ["INGEST_MODE"] = "offline"``, which only helps if their import
      happens to come after the module that loaded the ``.env``.

    So each file passed in isolation while the whole directory produced 74 failures
    (``SlackIngestError``/``TeamsIngestError``/``ConfluenceIngestError``/
    ``SharePointIngestError``: "Live mode requires ... a token from the credential
    vault"), and which tests failed depended on the selection and its alphabetical
    order — the worst kind of red, because it looks like a real defect and moves
    when you try to isolate it.

    Pinning it per test removes import order from the equation. ``monkeypatch``
    restores the previous value afterwards, so the suite leaves the environment as
    it found it. A test that genuinely exercises LIVE mode overrides this in its own
    body or fixture, which runs after this autouse fixture.
    """
    monkeypatch.setenv("INGEST_MODE", "offline")


@pytest.fixture(autouse=True)
def _no_db_repo_structure_sink(monkeypatch):
    """Default the graph-facing structure sink to in-memory no-ops (offline)."""
    try:
        import app.repo_structure_store as store
    except Exception:  # pragma: no cover — app not importable in a minimal env
        return
    monkeypatch.setattr(store, "persist_repo_structure", lambda org_id, structure: None)
    monkeypatch.setattr(store, "load_repo_structure", lambda org_id, repo_id: None)
