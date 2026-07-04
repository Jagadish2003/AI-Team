"""Provision the complete AgentIQ schema onto a target PostgreSQL database.

This is the MAINTAINED provisioning path (Alembic is the single source of truth
for the native tables). It is idempotent and safe to re-run.

The AgentIQ schema is assembled from three sources; this script runs all three
so the target database is complete after one invocation:

  1. Alembic migrations (0001..head) — the native tables:
        telemetry_events, signal_snapshots, entities, users, login_attempts,
        orgs, entity_relationships, causal_hypotheses, audit_log,
        workspace_members  (+ the alembic_version stamp).
  2. seed_loader.py --no-reset — the {id, payload} tables and run scaffolding:
        connectors, uploads, runs, evidence, mappings, permissions,
        opportunities, audit_events, executive_reports, run_events, kv
        (and, with --seed, the core reference rows: connectors / mappings /
        permissions / uploads).
  3. Tables with no Alembic migration and not in seed_loader, created directly
     here. The application no longer creates tables at runtime, so provisioning
     is the only place these are created: credentials, nonces, oauth_nonces.

Prerequisite: the target database already exists and DATABASE_URL points at it.
(The agentiq role is created by the pure-SQL path's provision.sql; this Alembic
runbook connects with whatever role DATABASE_URL specifies.)

Usage (run from the backend/ directory, with the venv active):

    # full schema + core reference seed (default)
    DATABASE_URL=postgresql://agentiq:***@db-host:5432/agentiq \
        python database/provision/provision_schema.py

    # schema only, no seed rows
    python database/provision/provision_schema.py --no-seed

This script does NOT drop anything — unlike `seed_loader.py` run on its own,
which resets the public schema by default. It only creates-if-missing and
upserts, so an existing database is upgraded in place.
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
    if "@" in url and "://" in url:
        scheme, rest = url.split("://", 1)
        creds, host = rest.split("@", 1)
        user = creds.split(":", 1)[0]
        return f"{scheme}://{user}:***@{host}"
    return url


def run_migrations() -> None:
    """Apply all Alembic migrations up to head against DATABASE_URL."""
    from alembic import command as alembic_command
    from alembic.config import Config as AlembicConfig

    print("[1/3] migrations ...")
    cfg = AlembicConfig(str(_BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(_BACKEND_DIR / "migrations"))
    # Skip env.py's fileConfig() so Alembic does not emit its INFO log lines
    # (the migration banner / "transactional DDL" noise). The migrations still
    # run identically. env.py reads DATABASE_URL from the environment.
    cfg.config_file_name = None
    alembic_command.upgrade(cfg, "head")


def run_seed_loader(seed: bool) -> None:
    """Create the {id, payload} core tables (and seed reference rows if asked).

    With seed=True we run seed_loader.py --no-reset, which creates the core
    {id, payload} tables AND upserts the reference rows (connectors, mappings,
    permissions, uploads). --no-reset is critical: it stops seed_loader dropping
    the Alembic-managed tables created in step 1 (its default behaviour is a full
    public-schema reset).

    With seed=False we import seed_loader and call ensure_db() directly, which
    creates the same tables but inserts no rows.
    """
    if not seed:
        print("[2/3] core tables (schema only) ...")
        import psycopg2

        from database import seed_loader

        con = psycopg2.connect(DATABASE_URL)
        try:
            seed_loader.ensure_db(con)
        finally:
            con.close()
        return

    print("[2/3] core tables + seed ...")
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
    print("[3/3] lazy tables (credentials, nonces, oauth_nonces) ...")
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


def verify(verbose: bool) -> None:
    """Report the resulting table count + Alembic head (full list with --verbose)."""
    import psycopg2

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
    print(f"Provisioning schema -> {_redacted(DATABASE_URL)}")
    run_migrations()
    run_seed_loader(seed)
    ensure_lazy_tables()
    verify(verbose)


if __name__ == "__main__":
    if "--help" in sys.argv or "-h" in sys.argv:
        print(__doc__)
        sys.exit(0)
    main()
