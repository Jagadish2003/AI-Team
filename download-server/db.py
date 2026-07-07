"""
SQLite data layer for the download portal.
Tables: clients (email allow-list + expiry) and licenses (key + passcode).
"""

import hashlib
import hmac
import pathlib
import sqlite3
from datetime import date
from typing import Optional

from config import SECRET_KEY

DB_PATH = pathlib.Path(__file__).parent / "data" / "portal.db"


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS clients (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                email       TEXT    UNIQUE NOT NULL COLLATE NOCASE,
                expiry_date TEXT,
                is_active   INTEGER NOT NULL DEFAULT 1,
                created_at  TEXT    NOT NULL DEFAULT (date('now'))
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS licenses (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                email         TEXT NOT NULL COLLATE NOCASE,
                license_key   TEXT NOT NULL,
                passcode_hash TEXT NOT NULL,
                created_at    TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)


# ---------------------------------------------------------------------------
# Passcode hashing
# ---------------------------------------------------------------------------

def _hash(value: str) -> str:
    return hmac.new(SECRET_KEY.encode(), value.encode(), hashlib.sha256).hexdigest()


# ---------------------------------------------------------------------------
# Clients
# ---------------------------------------------------------------------------

def list_clients() -> list:
    with _conn() as c:
        return c.execute(
            "SELECT * FROM clients ORDER BY created_at DESC"
        ).fetchall()


def add_client(email: str, expiry_date: Optional[str] = None) -> None:
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO clients (email, expiry_date, is_active) VALUES (?, ?, 1)",
            (email.lower().strip(), expiry_date or None),
        )


def update_client(client_id: int, email: str,
                  expiry_date: Optional[str], is_active: int) -> None:
    with _conn() as c:
        c.execute(
            "UPDATE clients SET email=?, expiry_date=?, is_active=? WHERE id=?",
            (email.lower().strip(), expiry_date or None, is_active, client_id),
        )


def delete_client(client_id: int) -> None:
    with _conn() as c:
        c.execute("DELETE FROM clients WHERE id=?", (client_id,))


def is_email_allowed(email: str) -> bool:
    """True if email is in the allow-list, active, and not past expiry."""
    with _conn() as c:
        row = c.execute(
            "SELECT expiry_date FROM clients WHERE email=? AND is_active=1",
            (email.lower().strip(),),
        ).fetchone()
    if row is None:
        return False
    if row["expiry_date"] and row["expiry_date"] < date.today().isoformat():
        return False
    return True


# ---------------------------------------------------------------------------
# Licenses
# ---------------------------------------------------------------------------

def list_licenses() -> list:
    with _conn() as c:
        return c.execute(
            "SELECT * FROM licenses ORDER BY created_at DESC"
        ).fetchall()


def add_license(email: str, license_key: str, passcode: str) -> int:
    """Insert a new license row. Returns the new row id."""
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO licenses (email, license_key, passcode_hash) VALUES (?, ?, ?)",
            (email.lower().strip(), license_key.strip(), _hash(passcode)),
        )
        return cur.lastrowid


def delete_license(license_id: int) -> None:
    with _conn() as c:
        c.execute("DELETE FROM licenses WHERE id=?", (license_id,))


def verify_license(email: str, passcode: str) -> Optional[str]:
    """Return the license_key if email+passcode match, else None."""
    with _conn() as c:
        row = c.execute(
            "SELECT license_key FROM licenses WHERE email=? AND passcode_hash=?",
            (email.lower().strip(), _hash(passcode)),
        ).fetchone()
    return row["license_key"] if row else None
