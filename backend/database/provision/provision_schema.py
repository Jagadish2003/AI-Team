"""Provision the complete AgentIQ schema onto a target PostgreSQL database.

This is the MAINTAINED provisioning path (Alembic is the single source of truth
for the native tables). It is idempotent and safe to re-run.

The AgentIQ schema is assembled from three sources; this script runs all three
in dependency order so the target database is complete after one invocation:

  1. Core ``{id,payload}`` tables are materialised first (without seed rows), so
     migrations that extend ``runs`` and protect ``runs``/``kv`` can see them.
  2. Alembic migrations (0001..head) — the native tables:
        telemetry_events, signal_snapshots, entities, users, login_attempts,
        orgs, entity_relationships, causal_hypotheses, audit_log,
        workspace_members, org_licenses, and (R-1.9.1-L3, migration 0026) the
        vendor-side license_registry + append-only issuance_audit — among others
        added by later migrations  (+ the alembic_version stamp).
        This path runs `alembic upgrade head`, so it stays in lock-step with the
        pure-SQL provision.sql bundle (Path B), which is a snapshot of this head.
  3. seed_loader.py --no-reset — optional core reference seed rows:
        connectors, uploads, runs, evidence, mappings, permissions,
        opportunities, audit_events, executive_reports, run_events, kv
        (and, with --seed, the core reference rows: connectors / mappings /
        permissions / uploads).
  4. Tables with no Alembic migration and not in seed_loader, created directly
     here. The application no longer creates tables at runtime, so provisioning
     is the only place these are created: credentials, nonces, oauth_nonces.
  5. History/immutability privileges are re-applied after every table source has
     run, then the complete A1/A2/A3 contract is verified read-only.

Prerequisite: the target database already exists and DATABASE_URL points at it.
(The agentiq role is created by the pure-SQL path's provision.sql; this Alembic
runbook connects with whatever role DATABASE_URL specifies.)

Usage (run from the backend/ directory, with the venv active):

    # full schema + core reference seed (default)
    DATABASE_URL=postgresql://agentiq:***@db-host:5432/agentiq \
        python database/provision/provision_schema.py

    # schema only, no seed rows
    python database/provision/provision_schema.py --no-seed

    # DESTRUCTIVE: drop every table first, then recreate from scratch
    python database/provision/provision_schema.py --reset          # asks for confirmation
    python database/provision/provision_schema.py --reset --yes    # non-interactive (LOCAL db only)

By DEFAULT this script does NOT drop anything — unlike `seed_loader.py` run on its
own, which resets the public schema by default. It only creates-if-missing and
upserts, so an existing database is upgraded in place.

The opt-in `--reset` flag makes it DESTRUCTIVE: it drops the entire `public`
schema (every table, incl. alembic_version) and recreates it before provisioning,
so the database is rebuilt from scratch. `--reset` is IRREVERSIBLE and requires
typing the target database name to confirm. It is never implied — a plain
`provision.sh` run can never drop data by accident.

`--yes` skips the interactive confirmation, but ONLY for a LOCAL database
(localhost / 127.0.0.1 / unix socket). Against a remote host `--reset --yes` is
refused outright, so a CI/CD pipeline or runbook carrying a production
`DATABASE_URL` can never silently drop a production schema — a deliberate remote
reset must be run interactively and confirmed by typing the database name.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
_BACKEND_DIR = _THIS_DIR.parents[1]  # provision -> database -> backend

if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from dotenv import load_dotenv  # noqa: E402

# Honour backend/.env so DATABASE_URL can be supplied there too. An exported
# DATABASE_URL still wins (override=False).
load_dotenv(_BACKEND_DIR / ".env")

DATABASE_URL = os.getenv("DATABASE_URL")


def _redacted(url: str) -> str:
    """Hide the password in a connection string for logging."""
    try:
        from psycopg2.extensions import parse_dsn

        parts = parse_dsn(url)
        user = parts.get("user") or "(default)"
        host = parts.get("host") or "(local socket)"
        port = f":{parts['port']}" if parts.get("port") else ""
        database = parts.get("dbname") or "(default)"
        return f"postgresql://{user}:***@{host}{port}/{database}"
    except Exception:
        # Never return the original input on a parse failure: it can contain the
        # very password this helper exists to protect.
        return "<redacted database URL>"


def _db_name(url: str) -> str:
    """Extract the database name from a connection URL (for the confirm prompt)."""
    try:
        from psycopg2.extensions import parse_dsn

        return str(parse_dsn(url).get("dbname") or "")
    except Exception:
        return ""


#: Hosts treated as a local database — loopback, or a unix socket (no host).
_LOCAL_DB_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", ""})


def _db_host(url: str) -> str:
    """Return the lowercased host of a connection URL ('' for a unix socket)."""
    try:
        from psycopg2.extensions import parse_dsn

        return str(parse_dsn(url).get("host") or "").lower()
    except Exception:
        return ""


def _is_local_db(url: str) -> bool:
    """True when the URL points at a local database (loopback / unix socket).

    The guard for non-interactive ``--reset --yes``: a remote host (anything not in
    :data:`_LOCAL_DB_HOSTS`) is treated as potentially production and refused for the
    unattended destructive path.
    """
    try:
        from psycopg2.extensions import parse_dsn

        host = str(parse_dsn(url).get("host") or "").lower()
    except Exception:
        # A DSN we cannot prove is local is treated as remote/potentially
        # production.  This is the destructive-reset guard, so fail closed.
        return False
    return host in _LOCAL_DB_HOSTS


def confirm_reset() -> None:
    """Require explicit confirmation before the DESTRUCTIVE schema drop.

    Prints the (redacted) target and the database name, then requires the operator
    to type that exact name — unless ``--yes`` is passed for non-interactive use.
    This is the single guard that stops ``--reset`` wiping the wrong database (e.g.
    a production URL left in the environment).
    """
    name = _db_name(DATABASE_URL)
    print("")
    print("*** WARNING: --reset will DROP ALL TABLES in this database ***")
    print(f"    target   : {_redacted(DATABASE_URL)}")
    print(f"    database : {name}")
    print("    This is IRREVERSIBLE. Make sure you have a backup.")
    if "--yes" in sys.argv:
        # Production guard: the non-interactive path is allowed ONLY against a local
        # database. A remote host is treated as potentially production, so
        # `--reset --yes` is refused there — a CI/CD pipeline or runbook that carries
        # a production DATABASE_URL can never silently DROP the schema. A deliberate
        # remote reset is still possible, but only interactively (typing the name).
        if not _is_local_db(DATABASE_URL):
            raise SystemExit(
                "Refusing '--reset --yes' against a non-local database (host "
                f"'{_db_host(DATABASE_URL) or 'unknown'}'). Non-interactive reset is "
                "permitted only for a local database (localhost / 127.0.0.1 / unix "
                "socket) so an automated run can never destroy a remote or production "
                "schema. Re-run WITHOUT --yes and type the database name to confirm."
            )
        print("    (--yes supplied; local database; skipping interactive confirmation)")
        return
    try:
        entered = input(f'Type the database name "{name}" to proceed (or anything else to abort): ').strip()
    except EOFError:
        entered = ""
    if entered != name:
        raise SystemExit("reset aborted — confirmation did not match the database name.")


def reset_schema() -> None:
    """DROP and recreate the ``public`` schema — every table, from scratch.

    Drops the whole schema (CASCADE takes tables, indexes, the alembic_version
    stamp, and the pgvector extension with it), then recreates an empty ``public``
    and restores baseline grants so the connecting role and PUBLIC can use it. The
    subsequent migration run rebuilds everything (incl. re-creating the vector
    extension). Requires a role that owns / can drop the public schema (superuser,
    or the schema owner).
    """
    import psycopg2

    print("[0/5] reset: DROP SCHEMA public CASCADE; CREATE SCHEMA public ...")
    con = psycopg2.connect(DATABASE_URL)
    try:
        con.autocommit = True
        cur = con.cursor()
        cur.execute("DROP SCHEMA IF EXISTS public CASCADE")
        cur.execute("CREATE SCHEMA public")
        # Restore the baseline grants a fresh database ships with, so the app role
        # (and other roles) can use the recreated schema.
        cur.execute("GRANT ALL ON SCHEMA public TO CURRENT_USER")
        cur.execute("GRANT ALL ON SCHEMA public TO public")
    finally:
        con.close()


def run_migrations() -> None:
    """Apply all Alembic migrations up to head against DATABASE_URL."""
    from alembic import command as alembic_command
    from alembic.config import Config as AlembicConfig

    print("[2/5] migrations ...")
    cfg = AlembicConfig(str(_BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(_BACKEND_DIR / "migrations"))
    # Skip env.py's fileConfig() so Alembic does not emit its INFO log lines
    # (the migration banner / "transactional DDL" noise). The migrations still
    # run identically. env.py reads DATABASE_URL from the environment.
    cfg.config_file_name = None
    alembic_command.upgrade(cfg, "head")


def ensure_core_tables() -> None:
    """Create the non-Alembic core tables before versioned migrations run.

    ``runs`` and ``kv`` are owned by seed_loader, but migrations 0011/0044 extend
    or protect them.  Creating their base shape first prevents a clean install
    from silently skipping those migrations because the tables did not exist yet.
    """

    import io
    import psycopg2
    from contextlib import redirect_stdout

    print("[1/5] core tables (schema only) ...")
    # seed_loader historically prints DATABASE_URL at import. Suppress that
    # legacy output so provisioning never exposes a password in deployment logs.
    with redirect_stdout(io.StringIO()):
        from database import seed_loader

    con = psycopg2.connect(DATABASE_URL)
    try:
        seed_loader.ensure_db(con)
    finally:
        con.close()


def seed_core_rows(seed: bool) -> None:
    """Optionally upsert the core reference rows after migrations complete."""

    if not seed:
        print("[3/5] core reference seed skipped (--no-seed) ...")
        return

    print("[3/5] core reference seed ...")
    # seed_loader prints its own progress (Seed Path / counts); capture it and
    # surface it only on failure to keep the provisioning output concise.
    result = subprocess.run(
        [sys.executable, str(_BACKEND_DIR / "database" / "seed_loader.py"), "--no-reset"],
        cwd=str(_BACKEND_DIR),
        env={**os.environ, "DATABASE_URL": DATABASE_URL, "PYTHONIOENCODING": "utf-8"},
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode != 0:
        raise SystemExit(f"seed_loader failed:\n{result.stdout}\n{result.stderr}")


def ensure_lazy_tables() -> None:
    """Create credentials, nonces and oauth_nonces.

    These three have no Alembic migration and are not in seed_loader, so
    provisioning is the only place that creates them — the application no longer
    creates any tables at runtime. The DDL is executed directly here (not via
    app code) and shares the credentials schema constants with the app. Each
    statement is CREATE TABLE/INDEX IF NOT EXISTS (plus an idempotent ADD COLUMN
    IF NOT EXISTS), so this is safe to re-run.
    """
    print("[4/5] lazy tables (credentials, nonces, oauth_nonces) ...")
    import psycopg2

    from database.models.credentials import (
        ALTER_CREDENTIALS_ADD_IS_DELETED,
        ALTER_CREDENTIALS_ADD_REFRESH_FAILED,
        ALTER_CREDENTIALS_ADD_STATIC_CREDENTIAL_COLUMNS,
        CREATE_CREDENTIALS_IDX_CONNECTOR,
        CREATE_CREDENTIALS_IDX_ORG,
        CREATE_CREDENTIALS_TABLE,
    )

    con = psycopg2.connect(DATABASE_URL)
    try:
        cur = con.cursor()
        cur.execute(CREATE_CREDENTIALS_TABLE)
        cur.execute(CREATE_CREDENTIALS_IDX_ORG)
        cur.execute(CREATE_CREDENTIALS_IDX_CONNECTOR)
        cur.execute(ALTER_CREDENTIALS_ADD_REFRESH_FAILED)
        cur.execute(ALTER_CREDENTIALS_ADD_IS_DELETED)
        cur.execute(ALTER_CREDENTIALS_ADD_STATIC_CREDENTIAL_COLUMNS)
        cur.execute(
            "CREATE TABLE IF NOT EXISTS nonces ("
            "key TEXT PRIMARY KEY, data TEXT NOT NULL, "
            "is_deleted BOOLEAN NOT NULL DEFAULT FALSE)"
        )
        cur.execute(
            "ALTER TABLE nonces ADD COLUMN IF NOT EXISTS is_deleted BOOLEAN NOT NULL DEFAULT FALSE"
        )
        cur.execute(
            "CREATE TABLE IF NOT EXISTS oauth_nonces ("
            "nonce TEXT PRIMARY KEY, connector_id TEXT NOT NULL, "
            "expires_at TEXT NOT NULL, "
            "is_deleted BOOLEAN NOT NULL DEFAULT FALSE)"
        )
        cur.execute(
            "ALTER TABLE oauth_nonces ADD COLUMN IF NOT EXISTS is_deleted BOOLEAN NOT NULL DEFAULT FALSE"
        )
        con.commit()
    finally:
        con.close()


def apply_history_privileges() -> None:
    """Apply privilege guards after every table source has materialised.

    On a clean install ``kv``/``runs`` come from seed_loader rather than Alembic.
    Re-applying the idempotent guards here closes the ordering gap where migration
    0044 ran before those tables existed.
    """

    import psycopg2

    from database.models.closed_loop_immutability import (
        ALL_CLOSED_LOOP_IMMUTABILITY_DDL,
    )
    from database.models.history_retention import ALL_HISTORY_RETENTION_DDL

    print("[5/5] closed-loop history privileges ...")
    con = psycopg2.connect(DATABASE_URL)
    try:
        with con.cursor() as cur:
            for statement in (
                *ALL_HISTORY_RETENTION_DDL,
                *ALL_CLOSED_LOOP_IMMUTABILITY_DDL,
            ):
                cur.execute(statement)
        con.commit()
    finally:
        con.close()


def verify(verbose: bool) -> None:
    """Report inventory/head and fail if the A1/A2/A3 loop is incomplete."""
    import psycopg2

    from database.provision.a1_a3_readiness import inspect_connection

    con = psycopg2.connect(DATABASE_URL)
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' ORDER BY table_name"
        )
        tables = [r[0] for r in cur.fetchall()]
        cur.execute("SELECT version_num FROM alembic_version")
        version = cur.fetchone()
        head = version[0] if version else "(none)"
        print(f"Done: {len(tables)} tables, alembic head {head}.")
        readiness = inspect_connection(con)
        if not readiness.ready:
            detail = "\n".join(f"  - {issue}" for issue in readiness.issues)
            raise SystemExit(
                "Provisioning completed, but the A1/A2/A3 database contract is "
                f"not ready:\n{detail}"
            )
        print("A1/A2/A3 database readiness: READY.")
        if verbose:
            print("  " + ", ".join(tables))
    finally:
        con.close()


def main() -> None:
    if not DATABASE_URL:
        raise SystemExit(
            "DATABASE_URL is not set (export it or add it to backend/.env)."
        )
    seed = "--no-seed" not in sys.argv
    verbose = "--verbose" in sys.argv or "-v" in sys.argv
    reset = "--reset" in sys.argv
    print(f"Provisioning schema -> {_redacted(DATABASE_URL)}")
    if reset:
        confirm_reset()
        reset_schema()
    ensure_core_tables()
    run_migrations()
    seed_core_rows(seed)
    ensure_lazy_tables()
    apply_history_privileges()
    verify(verbose)


if __name__ == "__main__":
    if "--help" in sys.argv or "-h" in sys.argv:
        print(__doc__)
        sys.exit(0)
    main()
