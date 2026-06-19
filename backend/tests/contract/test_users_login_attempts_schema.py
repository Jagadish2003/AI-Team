"""Schema contract tests for the users and login_attempts tables — AT-232.

Ported to PostgreSQL (AT-288 / Fix 1): introspection uses information_schema /
pg_indexes instead of SQLite PRAGMA. The conftest migrates the test database to
head before the suite runs, so this proves migration 0004 applied cleanly.

Covers:
  AC1  — users has NO org_id and NO role column; identity lives here, role and
         org_id live in workspace_members only.
  AC16 — (schema side) password_hash column exists, is NOT NULL, and stores a
         bcrypt hash beginning with '$2b$12$' verbatim with no plaintext.
"""
import os

import psycopg2
import pytest

import sqlite3  # routed to PostgreSQL by conftest

# (name, data_type, character_maximum_length, required/NOT NULL, primary_key)
# reset_token_hash / reset_token_expires_at are appended last by the CS-3
# password-reset migration (0012 in this integration line) and also live at the
# end of CREATE_USERS_TABLE, so migrated and fresh schemas align on ordinal order.
EXPECTED_USERS_COLUMNS = [
    ("id", "character varying", 36, True, True),
    ("email", "character varying", 256, True, False),
    ("password_hash", "character varying", 256, True, False),
    ("is_active", "boolean", None, True, False),
    ("invite_token_hash", "character varying", 256, False, False),
    ("invite_token_expires_at", "timestamp without time zone", None, False, False),
    ("created_at", "timestamp without time zone", None, True, False),
    ("last_login_at", "timestamp without time zone", None, False, False),
    ("reset_token_hash", "character varying", 256, False, False),
    ("reset_token_expires_at", "timestamp without time zone", None, False, False),
]

EXPECTED_LOGIN_ATTEMPTS_COLUMNS = [
    ("id", "character varying", 36, True, True),
    ("email", "character varying", 256, True, False),
    ("ip_address", "character varying", 64, True, False),
    ("attempted_at", "timestamp without time zone", None, True, False),
    ("succeeded", "boolean", None, True, False),
]

# Columns that must NEVER appear on users — they belong to workspace_members.
FORBIDDEN_USERS_COLUMNS = {"org_id", "role"}


@pytest.fixture()
def conn():
    connection = sqlite3.connect("")  # conftest routes this to PostgreSQL
    try:
        yield connection
    finally:
        connection.close()


def _columns(conn, table):
    return conn.execute(
        """
        SELECT column_name, data_type, character_maximum_length, is_nullable
        FROM information_schema.columns
        WHERE table_name = %s
        ORDER BY ordinal_position
        """,
        (table,),
    ).fetchall()


def _primary_key_columns(conn, table):
    rows = conn.execute(
        """
        SELECT a.attname
        FROM pg_index i
        JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
        WHERE i.indrelid = %s::regclass AND i.indisprimary
        """,
        (table,),
    ).fetchall()
    return {r[0] for r in rows}


def _assert_columns(conn, table, expected):
    rows = _columns(conn, table)
    assert [r["column_name"] for r in rows] == [c[0] for c in expected]
    pk_cols = _primary_key_columns(conn, table)
    for row, (name, data_type, char_len, required, primary_key) in zip(rows, expected):
        assert row["column_name"] == name
        assert row["data_type"] == data_type
        assert row["character_maximum_length"] == char_len
        assert (row["is_nullable"] == "NO") is required
        assert (name in pk_cols) is primary_key


def test_users_schema_matches_spec(conn):
    _assert_columns(conn, "users", EXPECTED_USERS_COLUMNS)


def test_users_has_no_org_id_or_role_column(conn):
    """AC1 — identity only. org_id and role live in workspace_members."""
    columns = {r["column_name"] for r in _columns(conn, "users")}
    assert FORBIDDEN_USERS_COLUMNS.isdisjoint(columns), (
        f"users must not contain {FORBIDDEN_USERS_COLUMNS & columns}; "
        "role/org_id belong to workspace_members only"
    )


def test_users_email_unique_index_exists(conn):
    index_defs = {
        r["indexname"]: r["indexdef"]
        for r in conn.execute(
            "SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'users'"
        ).fetchall()
    }
    assert "idx_users_email_unique" in index_defs
    indexdef = index_defs["idx_users_email_unique"]
    assert "UNIQUE INDEX" in indexdef
    assert "(email)" in indexdef


def test_users_email_uniqueness_is_enforced(conn):
    conn.execute(
        """
        INSERT INTO users (id, email, password_hash, is_active, created_at)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (
            "11111111-1111-1111-1111-111111111111",
            "owner@example.com",
            "$2b$12$abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUV0123456",
            True,
            "2026-06-09T10:00:00+00:00",
        ),
    )
    with pytest.raises(psycopg2.IntegrityError):
        conn.execute(
            """
            INSERT INTO users (id, email, password_hash, is_active, created_at)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (
                "22222222-2222-2222-2222-222222222222",
                "owner@example.com",
                "$2b$12$ZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZ0",
                True,
                "2026-06-09T11:00:00+00:00",
            ),
        )
    conn.rollback()


def test_password_hash_stores_bcrypt_no_plaintext(conn):
    """AC16 (schema side) — column stores the bcrypt hash verbatim, never plaintext."""
    bcrypt_hash = "$2b$12$C6UzMDM.H6dfI/f/IKcEeO3a8b9c0d1e2f3g4h5i6j7k8l9m0n1o2"
    conn.execute(
        """
        INSERT INTO users (id, email, password_hash, is_active, created_at)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (
            "33333333-3333-3333-3333-333333333333",
            "analyst@example.com",
            bcrypt_hash,
            True,
            "2026-06-09T12:00:00+00:00",
        ),
    )
    stored = conn.execute(
        "SELECT password_hash FROM users WHERE email = %s", ("analyst@example.com",)
    ).fetchone()["password_hash"]

    assert stored == bcrypt_hash
    assert stored.startswith("$2b$12$")
    conn.rollback()


def test_password_hash_is_required(conn):
    with pytest.raises(psycopg2.IntegrityError):
        conn.execute(
            """
            INSERT INTO users (id, email, password_hash, is_active, created_at)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (
                "44444444-4444-4444-4444-444444444444",
                "nopass@example.com",
                None,
                True,
                "2026-06-09T13:00:00+00:00",
            ),
        )
    conn.rollback()


def test_login_attempts_schema_matches_spec(conn):
    _assert_columns(conn, "login_attempts", EXPECTED_LOGIN_ATTEMPTS_COLUMNS)


def test_login_attempts_lookup_indexes_exist(conn):
    index_defs = {
        r["indexname"]: r["indexdef"]
        for r in conn.execute(
            "SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'login_attempts'"
        ).fetchall()
    }
    assert {"idx_login_attempts_email", "idx_login_attempts_ip"}.issubset(set(index_defs))
    # Column order inside each composite index def.
    assert index_defs["idx_login_attempts_email"].find("email") < index_defs[
        "idx_login_attempts_email"
    ].find("attempted_at")
    assert index_defs["idx_login_attempts_ip"].find("ip_address") < index_defs[
        "idx_login_attempts_ip"
    ].find("attempted_at")
