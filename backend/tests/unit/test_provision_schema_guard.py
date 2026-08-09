"""
Review finding #4 — provision_schema.py `--reset --yes` production guard.

The non-interactive schema-reset path (`--reset --yes`) drops the entire public
schema. It must be refused against a non-local (potentially production) database so
a CI/CD pipeline or runbook carrying a production DATABASE_URL can never silently
destroy the schema. A deliberate remote reset is still possible, but only
interactively (typing the database name to confirm).

``provision_schema`` calls ``load_dotenv(backend/.env)`` at import, which can set
``INGEST_MODE`` / ``DATABASE_URL`` from the dev ``.env``. To keep that side effect
out of the shared test session, the module is imported lazily inside the ``ps``
fixture, which snapshots and restores ``INGEST_MODE`` around the (one-time) import.
"""
from __future__ import annotations

import os
import sys

import pytest


@pytest.fixture
def ps(monkeypatch):
    """Import provision_schema without leaking its import-time env side effects."""
    pre = os.environ.get("INGEST_MODE")
    from database.provision import provision_schema as module

    # If the import's load_dotenv changed INGEST_MODE, restore the prior value so
    # it never leaks into other tests (monkeypatch reverts at teardown).
    if os.environ.get("INGEST_MODE") != pre:
        if pre is None:
            monkeypatch.delenv("INGEST_MODE", raising=False)
        else:
            monkeypatch.setenv("INGEST_MODE", pre)
    return module


@pytest.mark.parametrize(
    "url,is_local",
    [
        ("postgresql://u:p@localhost:5432/agentiq", True),
        ("postgresql://u:p@127.0.0.1:5432/agentiq", True),
        ("postgresql://u:p@[::1]:5432/agentiq", True),
        ("postgresql:///agentiq", True),  # unix socket — no host
        ("postgresql://u:p@prod-db.internal.example.com:5432/agentiq", False),
        ("postgresql://u:p@10.0.0.5:5432/agentiq", False),
    ],
)
def test_is_local_db(ps, url, is_local):
    assert ps._is_local_db(url) is is_local


def test_reset_yes_refused_against_remote_db(ps, monkeypatch):
    monkeypatch.setattr(ps, "DATABASE_URL", "postgresql://u:p@prod-db.example.com:5432/agentiq")
    monkeypatch.setattr(sys, "argv", ["provision_schema.py", "--reset", "--yes"])
    with pytest.raises(SystemExit) as exc:
        ps.confirm_reset()
    assert "non-local" in str(exc.value).lower()


def test_reset_yes_allowed_against_local_db(ps, monkeypatch):
    monkeypatch.setattr(ps, "DATABASE_URL", "postgresql://u:p@localhost:5432/agentiq")
    monkeypatch.setattr(sys, "argv", ["provision_schema.py", "--reset", "--yes"])
    # Local + --yes → skips confirmation and returns without raising.
    ps.confirm_reset()


def test_reset_interactive_remote_allowed_with_matching_name(ps, monkeypatch):
    # WITHOUT --yes, a remote reset is permitted only if the operator types the name.
    monkeypatch.setattr(ps, "DATABASE_URL", "postgresql://u:p@prod-db.example.com:5432/agentiq")
    monkeypatch.setattr(sys, "argv", ["provision_schema.py", "--reset"])
    monkeypatch.setattr("builtins.input", lambda *_a, **_k: "agentiq")
    ps.confirm_reset()  # name matches → returns without raising


def test_reset_interactive_aborts_on_wrong_name(ps, monkeypatch):
    monkeypatch.setattr(ps, "DATABASE_URL", "postgresql://u:p@localhost:5432/agentiq")
    monkeypatch.setattr(sys, "argv", ["provision_schema.py", "--reset"])
    monkeypatch.setattr("builtins.input", lambda *_a, **_k: "not-the-name")
    with pytest.raises(SystemExit):
        ps.confirm_reset()


def test_dsn_helpers_do_not_leak_special_character_password(ps):
    url = "postgresql://app:p%40ss%2Fword@db.example.com:5432/agentiq?sslmode=require"
    rendered = ps._redacted(url)
    assert "p%40ss" not in rendered
    assert "word" not in rendered
    assert "***" in rendered
    assert ps._db_name(url) == "agentiq"
    assert ps._db_host(url) == "db.example.com"


def test_unparseable_dsn_is_never_echoed(ps):
    secret = "definitely-not-a-valid-dsn-with-secret"
    assert ps._redacted(secret) == "<redacted database URL>"
    assert secret not in ps._redacted(secret)
    assert ps._is_local_db(secret) is False
