"""Alembic environment — AgentIQ 2.0.

Uses raw-SQL migrations only (no ORM models).  target_metadata is None.

DB URL resolution order:
  1. DB_PATH env var  → sqlite:////<absolute path>
  2. alembic.ini sqlalchemy.url key (default: sqlite:///database/dev.db)

This lets tests and CI redirect to a temp DB by setting DB_PATH without
modifying alembic.ini.
"""
from __future__ import annotations

import os
from logging.config import fileConfig

from sqlalchemy import create_engine, pool
from alembic import context

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
# DB URL override via DB_PATH env var.
# ---------------------------------------------------------------------------

_db_path_env = os.getenv("DB_PATH")
if _db_path_env:
    # Use an absolute path so Alembic resolves it correctly regardless of cwd.
    _abs = os.path.abspath(_db_path_env)
    config.set_main_option("sqlalchemy.url", f"sqlite:///{_abs}")


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
