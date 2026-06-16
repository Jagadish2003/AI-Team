"""Alembic environment — AgentIQ 2.0.

Uses raw-SQL migrations only (no ORM models).  target_metadata is None.

DB URL resolution (AT-288 / Fix 1):
  1. DATABASE_URL env var (PostgreSQL connection string).
  2. The documented local default if DATABASE_URL is unset.

The resolved value is written into the Alembic config via set_main_option()
before the ``%(DATABASE_URL)s`` placeholder in alembic.ini is interpolated, so
tests and CI can redirect migrations by exporting DATABASE_URL without editing
alembic.ini.
"""
from __future__ import annotations

import os
from logging.config import fileConfig
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine, pool
from alembic import context

# Load backend/.env so `alembic upgrade head` honours DATABASE_URL without the
# caller exporting it. backend/ is the parent of this migrations/ directory.
# override=False — an exported DATABASE_URL (and the value pytest's conftest sets
# in os.environ) still takes precedence.
load_dotenv(Path(__file__).resolve().parents[1] / ".env")

# ---------------------------------------------------------------------------
# Alembic config object — gives access to values in alembic.ini.
# ---------------------------------------------------------------------------

config = context.config

# Apply ini-file logging config. disable_existing_loggers=False is required: the
# default (True) disables every logger created before this call, which silently
# breaks application logging — and pytest's caplog capture — for any code already
# imported when a migration runs mid-process (e.g. a test that calls
# command.upgrade after other modules have created their loggers).
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# ---------------------------------------------------------------------------
# No ORM — all migrations use op.execute() with raw SQL.
# ---------------------------------------------------------------------------

target_metadata = None

# ---------------------------------------------------------------------------
# DB URL — resolved from DATABASE_URL (PostgreSQL). Set the concrete value here
# so the ``%(DATABASE_URL)s`` placeholder in alembic.ini is never interpolated
# by ConfigParser (which would look in the ini, not the environment).
# ---------------------------------------------------------------------------

_database_url = os.getenv(
    "DATABASE_URL", "postgresql://agentiq:agentiq@localhost:5432/agentiq"
)
config.set_main_option("sqlalchemy.url", _database_url)


# ---------------------------------------------------------------------------
# Migration runners
# ---------------------------------------------------------------------------

def run_migrations_offline() -> None:
    """Emit SQL to stdout without a live DB connection (--sql flag)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live DB connection (default mode)."""
    connectable = create_engine(
        config.get_main_option("sqlalchemy.url"),
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
