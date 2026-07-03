import os
import subprocess
import sys
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

# AT-288 / Fix 1: contract tests run against PostgreSQL. The test database is a
# DEDICATED, disposable database (never the dev DB) — the schema is dropped and
# rebuilt from migrations at session start, so the suite stays hermetic without a
# throwaway SQLite file. _resolve_test_database_url() picks/derives that test DB;
# the helpers below decompose and rebuild the DSN robustly (psycopg2's parser, so
# special characters in the password don't break URL manipulation).
load_dotenv(BACKEND_DIR / ".env")


def _dsn_parts(url: str) -> dict:
    """Decompose a postgres URL or key=value DSN into a params dict.

    Uses psycopg2's parser so a password containing URL-special characters
    (``&``, ``%``, ``:`` …) is handled correctly — stdlib ``urlsplit`` mis-parses
    those (e.g. a ``&`` in the password leaks into the port).
    """
    from psycopg2.extensions import parse_dsn

    try:
        return parse_dsn(url)
    except Exception:
        return {}


def _db_name_of(url: str) -> str:
    """Return the database name from a postgres URL/DSN."""
    return _dsn_parts(url).get("dbname", "")


def _db_user_of(url: str) -> str:
    return _dsn_parts(url).get("user", "agentiq")


def _with_db_name(url: str, new_db: str) -> str:
    """Return ``url`` with its database name replaced by ``new_db``.

    Always rebuilds a clean, percent-encoded URL form so the result is accepted
    by BOTH psycopg2 and SQLAlchemy (alembic's create_engine) regardless of
    special characters in the credentials.
    """
    from urllib.parse import quote

    p = _dsn_parts(url)
    user = p.get("user", "")
    password = p.get("password", "")
    host = p.get("host", "localhost")
    port = p.get("port", "5432")
    auth = ""
    if user:
        auth = quote(user, safe="")
        if password:
            auth += ":" + quote(password, safe="")
        auth += "@"
    return f"postgresql://{auth}{host}:{port}/{new_db}"


def _resolve_test_database_url() -> str:
    """Resolve the database the contract suite runs against — NEVER the dev DB.

    The suite drops and rebuilds the public schema, so it must point at a
    dedicated, disposable test database. Precedence:

      1. ``TEST_DATABASE_URL`` — explicit override (use whatever it names).
      2. ``DATABASE_URL`` whose db name already ends in ``_test`` — used as-is
         (this is the CI case: ``agentiq_test``).
      3. Otherwise ``DATABASE_URL`` is a real/dev database (e.g. ``agentiqdev``):
         redirect to a sibling ``<db>_test`` so the dev DB is never touched.

    This is what stops a local ``pytest`` from wiping or polluting the developer's
    dev database (the cause of the License page flipping to "invalid" after a test
    run). ``_ensure_test_database_exists`` then creates the sibling DB if missing.
    """
    explicit = os.environ.get("TEST_DATABASE_URL")
    if explicit:
        # Rebuild to a clean URL form (handles special chars in credentials).
        return _with_db_name(explicit, _db_name_of(explicit))

    base = (
        os.environ.get("DATABASE_URL")
        or "postgresql://agentiq:agentiq@localhost:5432/agentiq_test"
    )
    name = _db_name_of(base)
    target = name if name.endswith("_test") else f"{name}_test"
    # Always rebuild a clean URL; redirect a dev DB → <db>_test so the dev DB
    # (e.g. agentiqdev) is never touched, while CI's agentiq_test stays as-is.
    return _with_db_name(base, target)


TEST_DATABASE_URL = _resolve_test_database_url()

# Keep contract tests hermetic even when backend/.env contains live-mode
# settings or a real LLM API key. Test modules import app.main at module scope,
# so these must be set before pytest imports those modules.
os.environ.setdefault("DEV_JWT", "dev-token-change-me")
os.environ["DATABASE_URL"] = TEST_DATABASE_URL
os.environ["INGEST_MODE"] = "offline"
os.environ["ANTHROPIC_API_KEY"] = ""
os.environ["AGENTIQ_DISABLE_BACKGROUND_JOBS"] = "1"
# OAUTH_CALLBACK_ALLOW_UNAUTH is a local-dev convenience that lets the OAuth
# callback complete without a Bearer header (a provider's browser redirect can
# carry none). It must NOT be active under tests, or the Bearer-required
# behaviour (AC17) cannot be asserted. Force it off regardless of backend/.env.
os.environ["OAUTH_CALLBACK_ALLOW_UNAUTH"] = ""

# Hermetic email: never contact a real SMTP server during tests. Per-test
# overrides via monkeypatch.setenv (e.g. test_email_service.py) still apply.
os.environ["EMAIL_PROVIDER"] = "noop"


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


def _ensure_test_database_exists(url: str) -> None:
    """Create the dedicated test database if it does not exist.

    Connects to a maintenance database (``postgres``/``template1``) with the same
    credentials and issues ``CREATE DATABASE`` when the target is missing. If the
    role lacks ``CREATEDB`` (common for a locked-down dev role), raises a clear,
    actionable error with the one-time SQL to run — instead of silently falling
    back to the dev DB.
    """
    from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

    name = _db_name_of(url)
    # Already reachable? Then it exists — nothing to do.
    try:
        psycopg2.connect(url).close()
        return
    except psycopg2.OperationalError:
        pass  # likely "database does not exist" — try to create it below

    last_exc = None
    for maint in ("postgres", "template1"):
        try:
            con = psycopg2.connect(_with_db_name(url, maint))
        except psycopg2.Error as exc:
            last_exc = exc
            continue
        try:
            con.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
            with con.cursor() as cur:
                cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,))
                if cur.fetchone():
                    return
                cur.execute(f'CREATE DATABASE "{name}"')
            print(f"[conftest] created test database {name!r}", file=sys.stderr)
            return
        except psycopg2.Error as exc:
            if getattr(exc, "pgcode", None) == "42501" or "permission denied" in str(exc).lower():
                raise RuntimeError(
                    f"Contract tests need a dedicated test database {name!r}, but the "
                    f"current role cannot create it (no CREATEDB privilege).\n"
                    f"Create it once (as a superuser / in pgAdmin), then re-run:\n\n"
                    f'    CREATE DATABASE "{name}" OWNER {_db_user_of(url)!r};\n\n'
                    f"Or point the suite at an existing test DB via TEST_DATABASE_URL.\n"
                    f"The dev database is never used for tests by design."
                ) from exc
            raise
        finally:
            con.close()
    raise RuntimeError(
        f"Could not reach a maintenance database to create {name!r}: {last_exc}"
    )


def pytest_configure(config):
    """Reset and seed a fresh PostgreSQL schema before any contract tests run."""
    os.environ.setdefault("DEV_JWT", "dev-token-change-me")
    os.environ["DATABASE_URL"] = TEST_DATABASE_URL

    # SAFETY GUARD: never run the destructive reset against a non-test database.
    # _resolve_test_database_url already redirects a dev DB to <db>_test, but this
    # is the hard backstop in case of a misconfigured TEST_DATABASE_URL.
    _test_db = _db_name_of(TEST_DATABASE_URL)
    if "test" not in _test_db.lower():
        raise RuntimeError(
            f"Refusing to run contract tests against {_test_db!r}: the target "
            f"database name must contain 'test'. Set TEST_DATABASE_URL to a "
            f"disposable database (the suite drops and rebuilds its schema)."
        )
    _ensure_test_database_exists(TEST_DATABASE_URL)
    os.environ["INGEST_MODE"] = "offline"
    os.environ["ANTHROPIC_API_KEY"] = ""
    os.environ["AGENTIQ_DISABLE_BACKGROUND_JOBS"] = "1"
    os.environ["EMAIL_PROVIDER"] = "noop"  # no real SMTP during tests
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
        import logging

        # alembic's env.py calls logging.config.fileConfig(alembic.ini), which
        # attaches a StreamHandler(sys.stderr) to the ROOT logger. That handler
        # grabs the pre-pytest stderr and then prints every app WARN/ERROR inline
        # for the rest of the session, bypassing pytest's per-test log capture —
        # so the intentional errors raised by failure-path tests (audit failure,
        # "broken DB" health check, weak-JWT warning, …) leak to the console.
        # Snapshot the root handlers, run the migration, then remove whatever
        # fileConfig added so pytest's capture is the only handler left (errors
        # then surface only when a test actually fails).
        _root = logging.getLogger()
        _handlers_before = set(_root.handlers)
        try:
            alembic_cfg = AlembicConfig(str(BACKEND_DIR / "alembic.ini"))
            alembic_cfg.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
            alembic_command.upgrade(alembic_cfg, "head")
        except Exception as exc:
            raise RuntimeError(f"alembic upgrade failed:\n{exc}") from exc
        finally:
            for _h in list(_root.handlers):
                if _h not in _handlers_before:
                    _root.removeHandler(_h)

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

        # The credentials/nonces/oauth_nonces tables are NOT in migrations or
        # seed_loader — they used to be created lazily by the app at runtime.
        # That runtime DDL has been removed (schema is now owned by
        # database/provision/provision.sh), so create them here for the
        # resettable test DB. A pre-provisioned shared DB already has them.
        from database.models.credentials import (
            ALTER_CREDENTIALS_ADD_IS_DELETED,
            ALTER_CREDENTIALS_ADD_REFRESH_FAILED,
            CREATE_CREDENTIALS_IDX_CONNECTOR,
            CREATE_CREDENTIALS_IDX_ORG,
            CREATE_CREDENTIALS_TABLE,
        )

        _lazy_con = psycopg2.connect(os.environ["DATABASE_URL"])
        try:
            with _lazy_con.cursor() as _cur:
                _cur.execute(CREATE_CREDENTIALS_TABLE)
                _cur.execute(CREATE_CREDENTIALS_IDX_ORG)
                _cur.execute(CREATE_CREDENTIALS_IDX_CONNECTOR)
                _cur.execute(ALTER_CREDENTIALS_ADD_REFRESH_FAILED)
                _cur.execute(ALTER_CREDENTIALS_ADD_IS_DELETED)
                _cur.execute(
                    "CREATE TABLE IF NOT EXISTS nonces ("
                    "key TEXT PRIMARY KEY, data TEXT NOT NULL, "
                    "is_deleted BOOLEAN NOT NULL DEFAULT FALSE)"
                )
                _cur.execute(
                    "CREATE TABLE IF NOT EXISTS oauth_nonces ("
                    "nonce TEXT PRIMARY KEY, connector_id TEXT NOT NULL, "
                    "expires_at TEXT NOT NULL, "
                    "is_deleted BOOLEAN NOT NULL DEFAULT FALSE)"
                )
            _lazy_con.commit()
        finally:
            _lazy_con.close()

    # Seed the dev user as owner of the default org so legacy contract tests
    # pass with RBAC applied. The app's lifespan does this for context-managed
    # TestClients, but some test modules instantiate TestClient(app) directly
    # (no lifespan), so seed here to cover the whole suite.
    #
    # seed_owner() upserts the owner row using only INSERT/UPDATE — it does NOT
    # create the workspace_members table. The table is owned by the provisioning
    # layer (alembic on a resettable DB, or the pre-provisioned shared DB), so the
    # test harness performs no DDL here. This lets the suite run under a
    # least-privilege app role that has no CREATE on the public schema.
    from app.rbac import seed_owner

    seed_owner("default", os.environ["DEV_JWT"])

    # Guarantee a clean slate for high-churn tables at session start, even when
    # the schema could NOT be dropped above (a shared/provisioned DB where the
    # role lacks DROP privilege, so _reset_database() was skipped). On the
    # resettable path these tables were just recreated empty, so this is a no-op;
    # on a non-resettable DB it clears rows accumulated by PRIOR sessions that
    # otherwise inflate count-based assertions (e.g. expected 3, got 15) and
    # collide on fixed primary keys. DELETE needs only DELETE privilege, which
    # the app role already exercises at runtime. Children first to respect FKs;
    # each guarded so a table absent on this DB is simply skipped.
    for _table in (
        "entity_relationships",
        "entities",
        "signal_snapshots",
        "telemetry_events",
    ):
        _clean_con = psycopg2.connect(os.environ["DATABASE_URL"])
        try:
            _clean_con.autocommit = True
            with _clean_con.cursor() as _cur:
                _cur.execute(f"DELETE FROM {_table}")
        except psycopg2.Error:
            pass  # table missing or not deletable on this DB — skip
        finally:
            _clean_con.close()


@pytest.fixture(scope="session", autouse=True)
def _license_gate_valid_by_default():
    """LIC-1 / T5 (AT-346): run the whole contract session as if a valid license is installed.

    The shipped product carries a real CloudFulcrum-signed license; CI cannot
    mint one (no private key), so without this the license gate would resolve to
    read-only/invalid and 402 every discovery-run endpoint the suite exercises.
    Session-scoped so it also covers session/module-scoped fixtures that start
    runs during setup (e.g. completed_run_id). Tests that exercise the gate
    itself (test_license_gate.py) override this per-test with monkeypatch.
    """
    from unittest.mock import patch

    with patch(
        "app.middleware.license_gate.get_current_license_status",
        lambda *a, **k: {"status": "valid"},
    ):
        yield


@pytest.fixture(scope="session", autouse=True)
def _preserve_installed_license():
    """Snapshot and restore the per-org ``org_licenses`` rows around the session.

    The license contract tests write/clear rows in ``org_licenses`` directly.
    When the contract DB cannot be isolated — e.g. this environment lacks the
    privilege to reset the schema and falls back to the real provisioned DB —
    those writes land in the developer's dev database and clobber a license they
    pasted into the UI (symptom: the License page flips to "invalid" after a test
    run / restart).

    Snapshotting before the suite and restoring after means running the tests
    never permanently destroys an installed license. Within the session tests
    still manage their own license state; this only guarantees the pre-existing
    rows are put back at the end.
    """
    from app import db

    saved = None
    try:
        con = db.connect()
        try:
            cur = con.cursor()
            cur.execute(
                "SELECT org_id, license_key, last_seen_date, last_status "
                "FROM org_licenses"
            )
            saved = [tuple(r) for r in cur.fetchall()]
        finally:
            con.close()
    except Exception:
        saved = None  # DB/table not ready — nothing to preserve
    try:
        yield
    finally:
        if saved is not None:
            try:
                con = db.connect()
                try:
                    cur = con.cursor()
                    cur.execute("DELETE FROM org_licenses")
                    for org_id, license_key, last_seen, last_status in saved:
                        cur.execute(
                            "INSERT INTO org_licenses "
                            "(org_id, license_key, last_seen_date, last_status) "
                            "VALUES (%s, %s, %s, %s)",
                            (org_id, license_key, last_seen, last_status),
                        )
                    con.commit()
                finally:
                    con.close()
            except Exception:
                pass


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


def _reassert_auth_invariants() -> None:
    """Rebuild the auth-schema invariants the session bootstrap established.

    Recreates the auth tables (all DDL is ``IF NOT EXISTS``) and re-seeds the
    ``default`` org owner. Only ever repairs — a healthy schema is untouched.
    """
    from database.models.credentials import (
        ALTER_CREDENTIALS_ADD_IS_DELETED,
        ALTER_CREDENTIALS_ADD_REFRESH_FAILED,
        CREATE_CREDENTIALS_IDX_CONNECTOR,
        CREATE_CREDENTIALS_IDX_ORG,
        CREATE_CREDENTIALS_TABLE,
    )
    from database.models.workspace_members import CREATE_WORKSPACE_MEMBERS_TABLE

    con = psycopg2.connect(TEST_DATABASE_URL)
    try:
        with con.cursor() as cur:
            cur.execute(CREATE_WORKSPACE_MEMBERS_TABLE)
            cur.execute(CREATE_CREDENTIALS_TABLE)
            cur.execute(CREATE_CREDENTIALS_IDX_ORG)
            cur.execute(CREATE_CREDENTIALS_IDX_CONNECTOR)
            cur.execute(ALTER_CREDENTIALS_ADD_REFRESH_FAILED)
            cur.execute(ALTER_CREDENTIALS_ADD_IS_DELETED)
            cur.execute(
                "CREATE TABLE IF NOT EXISTS nonces ("
                "key TEXT PRIMARY KEY, data TEXT NOT NULL, "
                "is_deleted BOOLEAN NOT NULL DEFAULT FALSE)"
            )
            cur.execute(
                "CREATE TABLE IF NOT EXISTS oauth_nonces ("
                "nonce TEXT PRIMARY KEY, connector_id TEXT NOT NULL, "
                "expires_at TEXT NOT NULL, "
                "is_deleted BOOLEAN NOT NULL DEFAULT FALSE)"
            )
        con.commit()
    finally:
        con.close()

    from app.rbac import seed_owner

    seed_owner("default", os.environ.get("DEV_JWT", "dev-token-change-me"))


@pytest.fixture(autouse=True)
def _heal_shared_auth_schema():
    """Guard the session-shared DB against cross-test auth-schema pollution.

    The contract suite runs against ONE PostgreSQL schema for the whole session
    (see ``pytest_configure`` and the session-scoped ``client`` fixture) for
    speed. A few full-suite orderings leave the auth tables degraded — the
    ``credentials`` table dropped, or the ``default`` org owner seed removed —
    and because those invariants are set only once at session start, the damage
    persists and makes unrelated tenancy / RBAC / connector tests fail with a
    spurious ``403`` ("no role in org …") or ``UndefinedTable``.

    Re-assert the invariants before each test. A single cheap probe skips the
    (rare) repair path when everything is intact, so the steady-state cost is one
    ``SELECT``. All repair DDL is ``IF NOT EXISTS`` and the owner seed is an
    idempotent upsert, so a healthy schema is never modified — this only ever
    heals pollution left by an earlier test.
    """
    healthy = False
    try:
        con = psycopg2.connect(TEST_DATABASE_URL)
        try:
            con.autocommit = True
            with con.cursor() as cur:
                cur.execute(
                    "SELECT to_regclass('public.credentials'), "
                    "to_regclass('public.workspace_members'), "
                    "EXISTS(SELECT 1 FROM workspace_members "
                    "WHERE org_id='default' AND user_id=%s AND is_deleted=FALSE)",
                    (os.environ.get("DEV_JWT", "dev-token-change-me"),),
                )
                cred, wm, owner_ok = cur.fetchone()
            healthy = cred is not None and wm is not None and bool(owner_ok)
        finally:
            con.close()
    except Exception:
        healthy = False

    if not healthy:
        try:
            _reassert_auth_invariants()
        except Exception:
            # Never fail a test on the repair path itself — if the DB is truly
            # unusable the test will surface that on its own.
            pass

    yield
