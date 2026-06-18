"""Schema contract tests for the users and login_attempts tables — AT-232.

Covers:
  AC1  — users has NO org_id and NO role column; identity lives here, role and
         org_id live in workspace_members only. Verified by schema inspection.
  AC16 — (schema side) password_hash column exists, is NOT NULL, and stores a
         bcrypt hash beginning with '$2b$12$' verbatim with no plaintext.

The DB is built by running the real Alembic migrations to head, so this test
also proves migration 0004 applies cleanly on top of 0003.
"""
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest


BACKEND_DIR = Path(__file__).resolve().parents[2]

# (name, type, required/NOT NULL, primary_key)
# reset_token_hash / reset_token_expires_at are appended last by migration 0008
# (CS-3 forgot-password). Both nullable; SQLite ADD COLUMN appends at the end,
# matching their position in CREATE_USERS_TABLE so migrated and fresh schemas align.
EXPECTED_USERS_COLUMNS = [
    ("id", "VARCHAR(36)", True, True),
    ("email", "VARCHAR(256)", True, False),
    ("password_hash", "VARCHAR(256)", True, False),
    ("is_active", "BOOLEAN", True, False),
    ("invite_token_hash", "VARCHAR(256)", False, False),
    ("invite_token_expires_at", "TIMESTAMP", False, False),
    ("created_at", "TIMESTAMP", True, False),
    ("last_login_at", "TIMESTAMP", False, False),
    ("reset_token_hash", "VARCHAR(256)", False, False),
    ("reset_token_expires_at", "TIMESTAMP", False, False),
]

EXPECTED_LOGIN_ATTEMPTS_COLUMNS = [
    ("id", "VARCHAR(36)", True, True),
    ("email", "VARCHAR(256)", True, False),
    ("ip_address", "VARCHAR(64)", True, False),
    ("attempted_at", "TIMESTAMP", True, False),
    ("succeeded", "BOOLEAN", True, False),
]

# Columns that must NEVER appear on users — they belong to workspace_members.
FORBIDDEN_USERS_COLUMNS = {"org_id", "role"}


@pytest.fixture(scope="module")
def migrated_db_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    db_path = tmp_path_factory.mktemp("users_auth") / "auth.db"
    env = {
        **os.environ,
        "DB_PATH": str(db_path),
        "PYTHONIOENCODING": "utf-8",
    }
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "alembic.ini", "upgrade", "head"],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr or result.stdout
    return db_path


@pytest.fixture()
def conn(migrated_db_path: Path):
    connection = sqlite3.connect(str(migrated_db_path))
    connection.row_factory = sqlite3.Row
    try:
        yield connection
    finally:
        connection.close()


def test_users_schema_matches_spec(conn: sqlite3.Connection):
    rows = conn.execute("PRAGMA table_info(users)").fetchall()
    assert [row["name"] for row in rows] == [c[0] for c in EXPECTED_USERS_COLUMNS]

    for row, (name, expected_type, required, primary_key) in zip(
        rows, EXPECTED_USERS_COLUMNS
    ):
        assert row["name"] == name
        assert row["type"].upper() == expected_type
        assert bool(row["notnull"]) is required
        assert bool(row["pk"]) is primary_key


def test_users_has_no_org_id_or_role_column(conn: sqlite3.Connection):
    """AC1 — identity only. org_id and role live in workspace_members."""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
    assert FORBIDDEN_USERS_COLUMNS.isdisjoint(columns), (
        f"users must not contain {FORBIDDEN_USERS_COLUMNS & columns}; "
        "role/org_id belong to workspace_members only"
    )


def test_users_email_unique_index_exists(conn: sqlite3.Connection):
    index_rows = conn.execute("PRAGMA index_list(users)").fetchall()
    unique_email = next(
        (r for r in index_rows if r["name"] == "idx_users_email_unique"), None
    )
    assert unique_email is not None, "idx_users_email_unique missing"
    assert bool(unique_email["unique"]) is True

    columns = [
        r["name"]
        for r in conn.execute("PRAGMA index_xinfo(idx_users_email_unique)").fetchall()
        if r["key"] and r["name"] is not None
    ]
    assert columns == ["email"]


def test_users_email_uniqueness_is_enforced(conn: sqlite3.Connection):
    conn.execute(
        """
        INSERT INTO users (id, email, password_hash, is_active, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            "11111111-1111-1111-1111-111111111111",
            "owner@example.com",
            "$2b$12$abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUV0123456",
            1,
            "2026-06-09T10:00:00+00:00",
        ),
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO users (id, email, password_hash, is_active, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                "22222222-2222-2222-2222-222222222222",
                "owner@example.com",
                "$2b$12$ZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZ0",
                1,
                "2026-06-09T11:00:00+00:00",
            ),
        )


def test_password_hash_stores_bcrypt_no_plaintext(conn: sqlite3.Connection):
    """AC16 (schema side) — column stores the bcrypt hash verbatim, never plaintext."""
    bcrypt_hash = "$2b$12$C6UzMDM.H6dfI/f/IKcEeO3a8b9c0d1e2f3g4h5i6j7k8l9m0n1o2"
    conn.execute(
        """
        INSERT INTO users (id, email, password_hash, is_active, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            "33333333-3333-3333-3333-333333333333",
            "analyst@example.com",
            bcrypt_hash,
            1,
            "2026-06-09T12:00:00+00:00",
        ),
    )
    stored = conn.execute(
        "SELECT password_hash FROM users WHERE email = ?", ("analyst@example.com",)
    ).fetchone()["password_hash"]

    assert stored == bcrypt_hash
    assert stored.startswith("$2b$12$")


def test_password_hash_is_required(conn: sqlite3.Connection):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO users (id, email, password_hash, is_active, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                "44444444-4444-4444-4444-444444444444",
                "nopass@example.com",
                None,
                1,
                "2026-06-09T13:00:00+00:00",
            ),
        )


def test_login_attempts_schema_matches_spec(conn: sqlite3.Connection):
    rows = conn.execute("PRAGMA table_info(login_attempts)").fetchall()
    assert [row["name"] for row in rows] == [
        c[0] for c in EXPECTED_LOGIN_ATTEMPTS_COLUMNS
    ]

    for row, (name, expected_type, required, primary_key) in zip(
        rows, EXPECTED_LOGIN_ATTEMPTS_COLUMNS
    ):
        assert row["name"] == name
        assert row["type"].upper() == expected_type
        assert bool(row["notnull"]) is required
        assert bool(row["pk"]) is primary_key


def test_login_attempts_lookup_indexes_exist(conn: sqlite3.Connection):
    index_names = {
        r["name"] for r in conn.execute("PRAGMA index_list(login_attempts)").fetchall()
    }
    assert {"idx_login_attempts_email", "idx_login_attempts_ip"}.issubset(index_names)

    email_cols = [
        r["name"]
        for r in conn.execute("PRAGMA index_xinfo(idx_login_attempts_email)").fetchall()
        if r["key"] and r["name"] is not None
    ]
    ip_cols = [
        r["name"]
        for r in conn.execute("PRAGMA index_xinfo(idx_login_attempts_ip)").fetchall()
        if r["key"] and r["name"] is not None
    ]
    assert email_cols == ["email", "attempted_at"]
    assert ip_cols == ["ip_address", "attempted_at"]
