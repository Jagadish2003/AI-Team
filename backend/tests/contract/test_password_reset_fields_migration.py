"""Migration contract tests for the password-reset fields — CS-3 (migration 0008).

Acceptance criteria covered:
  * users gains nullable reset_token_hash + reset_token_expires_at columns.
  * Only a HASH is storable (the column is sized like the other token-hash
    columns); the schema never constrains it to a raw token.
  * The migration is SAFE FOR EXISTING USERS: a row inserted before 0008 (i.e.
    at revision 0007) survives the upgrade unchanged, with NULL reset fields and
    no backfill required.
  * Fresh-create (model CREATE_USERS_TABLE) and migrated (ALTER TABLE) schemas
    agree on column order — both append the two columns at the end.

These run Alembic against an isolated temp SQLite DB (DB_PATH), mirroring
test_users_login_attempts_schema.py.
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]

RESET_COLUMNS = ("reset_token_hash", "reset_token_expires_at")


def _alembic(db_path: Path, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "DB_PATH": str(db_path), "PYTHONIOENCODING": "utf-8"}
    return subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "alembic.ini", *args],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _users_columns(db_path: Path) -> dict[str, sqlite3.Row]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        return {r["name"]: r for r in conn.execute("PRAGMA table_info(users)").fetchall()}
    finally:
        conn.close()


# ── Schema shape at head ──────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def head_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    db_path = tmp_path_factory.mktemp("reset_head") / "auth.db"
    result = _alembic(db_path, "upgrade", "head")
    assert result.returncode == 0, result.stderr or result.stdout
    return db_path


def test_reset_columns_exist_and_are_nullable(head_db: Path):
    cols = _users_columns(head_db)
    for name in RESET_COLUMNS:
        assert name in cols, f"{name} missing after migration to head"
        # notnull == 0 → nullable, so existing rows need no backfill.
        assert cols[name]["notnull"] == 0, f"{name} must be nullable"
        # Not part of the primary key.
        assert cols[name]["pk"] == 0


def test_reset_columns_have_expected_types(head_db: Path):
    cols = _users_columns(head_db)
    assert cols["reset_token_hash"]["type"].upper() == "VARCHAR(256)"
    assert cols["reset_token_expires_at"]["type"].upper() == "TIMESTAMP"


def test_reset_columns_are_appended_last(head_db: Path):
    """Migrated order must match the model's CREATE_USERS_TABLE (both append last),
    so a DB created fresh from the model and one upgraded via ALTER agree."""
    names = list(_users_columns(head_db).keys())
    assert names[-2:] == ["reset_token_hash", "reset_token_expires_at"]


# ── Existing-user safety: 0007 → 0008 with a pre-existing row ─────────────────


def test_existing_user_survives_upgrade_with_null_reset_fields(
    tmp_path_factory: pytest.TempPathFactory,
):
    db_path = tmp_path_factory.mktemp("reset_existing") / "auth.db"

    # 1) Bring the DB up to the revision JUST BEFORE the reset migration.
    stepwise = _alembic(db_path, "upgrade", "0007")
    assert stepwise.returncode == 0, stepwise.stderr or stepwise.stdout

    # 2) Reproduce a genuinely LEGACY users table — one physically created before
    #    the reset columns existed. (Migration 0004 imports the model's
    #    CREATE_USERS_TABLE, which now already includes the reset columns, so a
    #    DB freshly built to 0007 has them. A real pre-existing production DB does
    #    not, so we drop them here to recreate that pre-0008 state authentically.)
    conn = sqlite3.connect(str(db_path))
    try:
        for col in RESET_COLUMNS:
            conn.execute(f"ALTER TABLE users DROP COLUMN {col}")
        conn.commit()
        pre_cols = {r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
        assert "reset_token_hash" not in pre_cols, "legacy table must lack reset cols"

        # Insert a user as it existed before 0008 (no reset columns).
        conn.execute(
            """
            INSERT INTO users (id, email, password_hash, is_active, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                "legacy@example.com",
                "$2b$12$C6UzMDM.H6dfI/f/IKcEeO3a8b9c0d1e2f3g4h5i6j7k8l9m0n1o2",
                1,
                "2026-06-09T10:00:00+00:00",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    # 3) Apply the reset migration — this exercises 0008's real ADD COLUMN branch.
    upgrade = _alembic(db_path, "upgrade", "head")
    assert upgrade.returncode == 0, upgrade.stderr or upgrade.stdout

    # 4) The legacy row is intact and its new reset fields default to NULL.
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM users WHERE email = ?", ("legacy@example.com",)
        ).fetchone()
        assert row is not None, "existing user must survive the migration"
        assert row["reset_token_hash"] is None
        assert row["reset_token_expires_at"] is None
        assert row["password_hash"].startswith("$2b$12$")  # untouched
    finally:
        conn.close()


def test_reset_fields_are_writable_and_clearable(head_db: Path):
    """The flow stores a hash + expiry, then clears them on consumption — both
    write paths must work against the migrated schema."""
    conn = sqlite3.connect(str(head_db))
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(
            """
            INSERT INTO users (id, email, password_hash, is_active, created_at,
                               reset_token_hash, reset_token_expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                "resettable@example.com",
                "$2b$12$C6UzMDM.H6dfI/f/IKcEeO3a8b9c0d1e2f3g4h5i6j7k8l9m0n1o2",
                1,
                "2026-06-09T10:00:00+00:00",
                "a" * 64,  # a SHA-256 hex digest is 64 chars — fits VARCHAR(256)
                "2026-06-09T11:00:00+00:00",
            ),
        )
        conn.commit()
        stored = conn.execute(
            "SELECT reset_token_hash, reset_token_expires_at FROM users WHERE email = ?",
            ("resettable@example.com",),
        ).fetchone()
        assert stored["reset_token_hash"] == "a" * 64
        assert stored["reset_token_expires_at"] == "2026-06-09T11:00:00+00:00"

        # Clear on consumption.
        conn.execute(
            "UPDATE users SET reset_token_hash = NULL, reset_token_expires_at = NULL "
            "WHERE email = ?",
            ("resettable@example.com",),
        )
        conn.commit()
        cleared = conn.execute(
            "SELECT reset_token_hash, reset_token_expires_at FROM users WHERE email = ?",
            ("resettable@example.com",),
        ).fetchone()
        assert cleared["reset_token_hash"] is None
        assert cleared["reset_token_expires_at"] is None
    finally:
        conn.close()


# ── Downgrade round-trip ──────────────────────────────────────────────────────


def test_downgrade_removes_reset_columns(tmp_path_factory: pytest.TempPathFactory):
    db_path = tmp_path_factory.mktemp("reset_downgrade") / "auth.db"
    assert _alembic(db_path, "upgrade", "head").returncode == 0

    cols_before = _users_columns(db_path)
    assert "reset_token_hash" in cols_before

    down = _alembic(db_path, "downgrade", "0007")
    assert down.returncode == 0, down.stderr or down.stdout

    cols_after = _users_columns(db_path)
    for name in RESET_COLUMNS:
        assert name not in cols_after, f"{name} should be dropped on downgrade"
    # The rest of the users table is still intact.
    assert "email" in cols_after and "password_hash" in cols_after
