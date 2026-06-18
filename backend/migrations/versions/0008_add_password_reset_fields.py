"""Add password-reset fields to the users table.

CS-3 — forgot/reset-password support.

Adds two NULLABLE columns to users:
  reset_token_hash        VARCHAR(256)  — SHA-256 hash of the reset token; the
                                          raw token is never stored, so a DB leak
                                          does not expose usable reset links.
  reset_token_expires_at  TIMESTAMP     — when the reset token becomes invalid
                                          (one hour after issue); the reset
                                          endpoint rejects expired links with 400.

Both columns are nullable, so the migration is safe for existing users — no
backfill is required and existing accounts stay valid (their reset fields are
simply NULL until they request a reset).

The two columns also live in the model's CREATE_USERS_TABLE
(database/models/users.py) for freshly created databases. Because the earlier
0004 migration builds users by importing that same model DDL, a brand-new DB
already has these columns by the time 0008 runs; an EXISTING DB (created before
the columns were added to the model) does not. So each ADD COLUMN here is applied
ONLY IF the column is absent — making the migration idempotent and correct on
both paths, and never drifting from the model that is the single source of truth.
This mirrors the import-from-model pattern used by 0003_create_entities.py and
0004_create_users_and_login_attempts.py. env.py declares this project "raw-SQL
migrations only"; importing tuples of raw SQL strings introduces no ORM metadata.

Revision ID: 0008
Revises: 0007
Create Date: 2026-06-18
"""
import os
import sys
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Maps each reset column to the model DDL constant that adds it, so the exact
# ADD COLUMN SQL stays sourced from database/models/users.py (no drift).
_RESET_COLUMNS = ("reset_token_hash", "reset_token_expires_at")


def _add_column_ddl() -> "dict[str, str]":
    """Return {column_name: ADD COLUMN SQL} from the model's single source."""
    backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    if backend_dir not in sys.path:
        sys.path.insert(0, backend_dir)
    from database.models.users import (
        ADD_RESET_TOKEN_HASH_COLUMN,
        ADD_RESET_TOKEN_EXPIRES_AT_COLUMN,
    )

    return {
        "reset_token_hash": ADD_RESET_TOKEN_HASH_COLUMN,
        "reset_token_expires_at": ADD_RESET_TOKEN_EXPIRES_AT_COLUMN,
    }


def _existing_user_columns() -> "set[str]":
    bind = op.get_bind()
    return {row[1] for row in bind.execute(sa.text("PRAGMA table_info(users)"))}


def upgrade() -> None:
    # Idempotent: 0004 may already have created these columns from the model on a
    # fresh DB. Only add a column that is genuinely missing (existing DBs).
    ddl = _add_column_ddl()
    present = _existing_user_columns()
    for column in _RESET_COLUMNS:
        if column not in present:
            op.execute(ddl[column])


def downgrade() -> None:
    # Reverse order of upgrade, and guard each drop so the downgrade is idempotent.
    # DROP COLUMN requires SQLite >= 3.35.0 (2021); the project ships a far newer
    # SQLite and PostgreSQL supports it natively.
    present = _existing_user_columns()
    for column in ("reset_token_expires_at", "reset_token_hash"):
        if column in present:
            op.execute(f"ALTER TABLE users DROP COLUMN {column}")
