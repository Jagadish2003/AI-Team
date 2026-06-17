import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import psycopg2
import psycopg2.extras
import pytest
from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from dotenv import load_dotenv
from fastapi.testclient import TestClient

BACKEND_DIR = Path(__file__).resolve().parents[2]
REPO_ROOT = BACKEND_DIR.parent
SEED_LOADER = BACKEND_DIR / "database" / "seed_loader.py"

for path in (str(REPO_ROOT), str(BACKEND_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

# AT-288 / Fix 1: contract tests run against PostgreSQL. The test database is
# taken from DATABASE_URL (or the documented local default). The schema is
# dropped and rebuilt from migrations at session start, so the suite stays
# hermetic without a throwaway SQLite file.
#
# Resolve DATABASE_URL the same way the app does: an exported env var wins, else
# backend/.env, else the documented local default. Without this fallback an
# unset DATABASE_URL left TEST_DATABASE_URL = None and crashed conftest import.
def _normalize_database_url(url: str) -> str:
    """Return a psycopg2-parseable DSN, percent-encoding a raw password if needed.

    A DATABASE_URL whose userinfo contains characters that are special in a
    libpq URI — most commonly a literal ``%`` or ``&`` in the password — fails
    psycopg2's URI parser with ``invalid percent-encoded token`` before any
    connection is attempted. Local ``backend/.env`` files often store the raw
    password, so this helper repairs the value at load time instead of forcing
    every developer to hand-encode their ``.env``.

    Idempotent: a URL that psycopg2 already accepts is returned unchanged; only
    a URL that fails to parse has its userinfo (username + password)
    percent-encoded and is then rebuilt. Non-URI ("key=value") DSNs and URLs
    without credentials are returned as-is.
    """
    import re
    from urllib.parse import quote

    from psycopg2.extensions import make_dsn

    if not url:
        return url

    try:
        make_dsn(url)
        return url  # already a valid DSN — leave it untouched
    except Exception:
        pass

    m = re.match(r"^(?P<scheme>[a-zA-Z][a-zA-Z0-9+.\-]*://)(?P<rest>.*)$", url, re.DOTALL)
    if not m:
        return url  # not a URI-form DSN (e.g. "host=... password=...")

    userinfo, at, hostpart = m.group("rest").rpartition("@")
    if not at:
        return url  # no credentials to encode

    user, sep, password = userinfo.partition(":")
    if not sep:
        return url  # username only, no password component

    rebuilt = f"{m.group('scheme')}{quote(user, safe='')}:{quote(password, safe='')}@{hostpart}"
    try:
        make_dsn(rebuilt)
    except Exception:
        return url  # encoding did not help — defer to the original error path
    return rebuilt


load_dotenv(BACKEND_DIR / ".env")
TEST_DATABASE_URL = _normalize_database_url(
    os.environ.get("DATABASE_URL")
    or "postgresql://agentiq:agentiq@localhost:5432/agentiq"
)

# Keep contract tests hermetic even when backend/.env contains live-mode
# settings or a real LLM API key. Test modules import app.main at module scope,
# so these must be set before pytest imports those modules.
os.environ.setdefault("DEV_JWT", "dev-token-change-me")
os.environ["DATABASE_URL"] = TEST_DATABASE_URL
os.environ["INGEST_MODE"] = "offline"
os.environ["ANTHROPIC_API_KEY"] = ""
os.environ["AGENTIQ_DISABLE_BACKGROUND_JOBS"] = "1"


# ---------------------------------------------------------------------------
# Test-only connection helper (NO SQL translation)
# ---------------------------------------------------------------------------
# The contract tests open their own DB connections for setup/assertions, written
# in the sqlite3 idiom: sqlite3.connect(path), then conn.execute(...) directly on
# the connection, and `with conn:` blocks. PostgreSQL/psycopg2 has neither a
# connection-level .execute() nor that file path.
#
# This helper provides ONLY those ergonomics — a connection-level execute() and
# context-manager support — over a real psycopg2 connection (DictCursor, so rows
# support both row[0] and row["col"]). It performs NO SQL translation: the test
# SQL itself is native PostgreSQL (%s placeholders, ON CONFLICT, information_schema,
# TRUE/FALSE). It is test infrastructure only; application code uses raw psycopg2
# cursors directly (see app/db.py). sqlite3.connect() is routed here so the test
# bodies need no per-call-site change, only their SQL converted to native.
import sqlite3 as _real_sqlite3  # noqa: E402


from psycopg2 import pool as _pg_pool  # noqa: E402

# Connection pool — the dominant cost of the suite was opening connections, not
# running queries: the test PostgreSQL accepts a new connection in ~0.28s and
# the app opens one per DB call (con = connect(); …; close()), so a single
# discovery run opened ~146 connections (~40s of pure handshake) and the suite
# tens of thousands (the ~20-minute runtime). Pooling pays the handshake a
# handful of times and reuses warm connections, with no change to application
# code or its open/commit/close semantics.
_CONN_POOL = None


def _get_pool():
    global _CONN_POOL
    if _CONN_POOL is None:
        _CONN_POOL = _pg_pool.ThreadedConnectionPool(
            1,
            64,
            os.environ["DATABASE_URL"],
            cursor_factory=psycopg2.extras.DictCursor,
            options="-c timezone=UTC",
        )
    return _CONN_POOL


class _PgTestConnection:
    """Pooled psycopg2 connection with sqlite3-style connection-level execute().

    Semantically equivalent to a fresh connection per call: each borrow is an
    isolated connection/transaction; close()/__exit__ return it to the pool
    after committing (clean exit) or rolling back (error), instead of physically
    closing it. NO query translation — SQL passes through verbatim.
    """

    def __init__(self, raw, pool):
        self._raw = raw
        self._pool = pool
        self._returned = False
        self.row_factory = None  # accepted + ignored; DictCursor gives name access

    def cursor(self, *args, **kwargs):
        return self._raw.cursor()

    def execute(self, sql, params=None):
        cur = self._raw.cursor()
        if params is None:
            cur.execute(sql)
        else:
            cur.execute(sql, params)
        return cur

    def executemany(self, sql, seq_of_params):
        cur = self._raw.cursor()
        cur.executemany(sql, list(seq_of_params))
        return cur

    def commit(self):
        self._raw.commit()

    def rollback(self):
        self._raw.rollback()

    def _release(self, errored=False):
        if self._returned:
            return
        self._returned = True
        try:
            if errored:
                self._raw.rollback()
        except Exception:
            pass
        try:
            self._pool.putconn(self._raw)
        except Exception:
            try:
                self._raw.close()
            except Exception:
                pass

    def close(self):
        self._release()

    @property
    def autocommit(self):
        return self._raw.autocommit

    @autocommit.setter
    def autocommit(self, value):
        self._raw.autocommit = value

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is not None:
            self._release(errored=True)
        else:
            try:
                self._raw.commit()
            finally:
                self._release()

    def __del__(self):
        # Safety net: several app helpers lack try/finally and skip close() when
        # a query raises. Return the connection on GC so the pool cannot leak.
        try:
            self._release()
        except Exception:
            pass


def pg_test_connect(*_args, **_kwargs):
    pool = _get_pool()
    raw = pool.getconn()
    # Reset any leftover state from the previous borrower before handing it out:
    # rollback first (clears an open/aborted txn), then pin autocommit off and
    # the UTC session the app expects.
    try:
        raw.rollback()
    except Exception:
        pass
    try:
        raw.autocommit = False
    except Exception:
        pass
    return _PgTestConnection(raw, pool)


# Route sqlite3.connect(...) in tests to PostgreSQL. Installed at import time so
# it is active before any test module's module-level sqlite3.connect() runs.
_real_sqlite3.connect = pg_test_connect

# Also route the application's own db.connect() through the same helper. Many
# test modules do `con = db.connect(); con.execute(...)` (the sqlite3 idiom).
# Patching here — before any test module (or app.main) imports `connect` — means
# both attribute access (db.connect()) and value-bound imports
# (from app.db import connect) resolve to the helper. Application code under test
# uses cursors, which the helper delegates to natively; only test code uses the
# helper's connection-level execute(). No SQL translation is performed.
import app.db as _app_db  # noqa: E402

_app_db.connect = pg_test_connect

# The telemetry write/read path opens its own connections via
# database/connection.py::_connect (psycopg2.connect directly), bypassing
# app.db.connect. Route it through the same pool so telemetry events during a
# run don't each pay the connection handshake. get_db_connection()/
# get_db_session() look up `_connect` as a module global at call time, so
# replacing the attribute is sufficient.
import database.connection as _db_conn_mod  # noqa: E402

_db_conn_mod._connect = pg_test_connect


def _resolve_seed_dir() -> Path:
    """Resolve SEED_DIR consistently from repo-root or backend working dirs."""
    raw_seed_dir = os.environ.get("SEED_DIR")
    if not raw_seed_dir:
        return BACKEND_DIR / "database" / "seed"

    seed_dir = Path(raw_seed_dir)
    if seed_dir.is_absolute():
        return seed_dir

    candidates = [
        (Path.cwd() / seed_dir).resolve(),
        (REPO_ROOT / seed_dir).resolve(),
        (BACKEND_DIR / seed_dir).resolve(),
    ]
    for candidate in candidates:
        if (candidate / "connectors.json").exists():
            return candidate
    return candidates[0]


_RESET_PUBLIC_SCHEMA = """
DO $$
DECLARE r RECORD;
BEGIN
    FOR r IN (SELECT tablename FROM pg_tables WHERE schemaname = 'public') LOOP
        EXECUTE 'DROP TABLE IF EXISTS public.' || quote_ident(r.tablename) || ' CASCADE';
    END LOOP;
    FOR r IN (SELECT sequencename FROM pg_sequences WHERE schemaname = 'public') LOOP
        EXECUTE 'DROP SEQUENCE IF EXISTS public.' || quote_ident(r.sequencename) || ' CASCADE';
    END LOOP;
END $$;
"""


def _reset_database() -> None:
    """Drop every table/sequence in the public schema for a clean slate."""
    con = psycopg2.connect(os.environ["DATABASE_URL"])
    try:
        con.autocommit = True
        with con.cursor() as cur:
            cur.execute(_RESET_PUBLIC_SCHEMA)
    finally:
        con.close()


def pytest_configure(config):
    """Reset and seed a fresh PostgreSQL schema before any contract tests run."""
    os.environ.setdefault("DEV_JWT", "dev-token-change-me")
    os.environ["DATABASE_URL"] = TEST_DATABASE_URL
    os.environ["INGEST_MODE"] = "offline"
    os.environ["ANTHROPIC_API_KEY"] = ""
    os.environ["AGENTIQ_DISABLE_BACKGROUND_JOBS"] = "1"
    os.environ["SEED_DIR"] = str(_resolve_seed_dir())
    os.environ.setdefault("CORS_ORIGINS", "http://localhost:5173")

    # The hermetic reset drops every public-schema table and rebuilds it from
    # migrations + seed. That is correct for a throwaway/CI database the test
    # role owns, but it must NOT run against a shared, pre-provisioned database
    # where the role does not own the schema objects: the DROP fails with
    # insufficient_privilege and, worse, would wipe shared data if it had rights.
    # When the role cannot reset, leave the existing schema in place and run the
    # suite against it (no deletion, no creation of tables/schema). The reset
    # runs as a single atomic DO-block, so a privilege failure drops nothing.
    schema_is_resettable = True
    try:
        _reset_database()
    except psycopg2.Error as exc:
        if getattr(exc, "pgcode", None) == "42501":  # insufficient_privilege
            schema_is_resettable = False
            print(
                "[conftest] DATABASE_URL role cannot reset the public schema "
                "(insufficient_privilege) - running contract tests against the "
                "existing provisioned schema; skipping schema drop/migrate/seed.",
                file=sys.stderr,
            )
        else:
            raise RuntimeError(
                "Could not reset the test PostgreSQL database at "
                f"{os.environ['DATABASE_URL']!r}. Ensure PostgreSQL is running and "
                f"the database/role exist.\n{exc}"
            ) from exc
    except Exception as exc:
        raise RuntimeError(
            "Could not reset the test PostgreSQL database at "
            f"{os.environ['DATABASE_URL']!r}. Ensure PostgreSQL is running and the "
            f"database/role exist.\n{exc}"
        ) from exc

    # Only (re)create the schema + reseed on a database this role owns (local
    # throwaway DB or CI). A pre-provisioned shared DB already has both.
    if schema_is_resettable:
        try:
            alembic_cfg = AlembicConfig(str(BACKEND_DIR / "alembic.ini"))
            alembic_cfg.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
            alembic_command.upgrade(alembic_cfg, "head")
        except Exception as exc:
            raise RuntimeError(f"alembic upgrade failed:\n{exc}") from exc

        # --no-reset: seed_loader now resets the public schema by default, but the
        # conftest already did its own reset + `alembic upgrade head` above, so the
        # seed step must NOT drop the migration tables it just created.
        result = subprocess.run(
            [sys.executable, str(SEED_LOADER), "--no-reset"],
            cwd=str(BACKEND_DIR),
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        if result.returncode != 0:
            raise RuntimeError(f"seed_loader.py failed:\n{result.stderr}")

    # Seed the dev user as owner of the default org so legacy contract tests
    # pass with RBAC applied. The app's lifespan does this for context-managed
    # TestClients, but some test modules instantiate TestClient(app) directly
    # (no lifespan), so seed here to cover the whole suite.
    from app.rbac import seed_owner

    seed_owner("default", os.environ["DEV_JWT"])

    from database.models.workspace_members import CREATE_WORKSPACE_MEMBERS_TABLE

    con = psycopg2.connect(os.environ["DATABASE_URL"])
    try:
        with con.cursor() as cur:
            cur.execute(CREATE_WORKSPACE_MEMBERS_TABLE)
            cur.execute(
                """
                INSERT INTO workspace_members (org_id, user_id, role, created_at)
                VALUES (%s, %s, 'owner', %s)
                ON CONFLICT (org_id, user_id) DO NOTHING
                """,
                (
                    "default",
                    os.environ["DEV_JWT"],
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
        con.commit()
    finally:
        con.close()


@pytest.fixture(scope="session")
def client():
    # Session-scoped: `with TestClient(app)` runs the app lifespan (seed_owner,
    # driver checks, ensure_auth_tables, overlay registration, several DB
    # connections) on enter. Function scope re-ran that whole startup for EVERY
    # test — ~0.6s each across ~2400 tests ≈ the 20-minute suite. The DB is
    # already shared for the whole session (reset once in pytest_configure), so
    # sharing one client adds no new cross-test state; the lifespan work is
    # idempotent. One startup instead of thousands.
    from app.main import app
    with TestClient(app) as c:
        yield c
