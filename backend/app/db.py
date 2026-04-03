import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional
from fastapi import HTTPException

DB_PATH = Path(os.getenv("DB_PATH", "database/dev.db"))

def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(str(DB_PATH))

def get_one(table: str, id_: str) -> Optional[Dict[str, Any]]:
    con = connect()
    cur = con.cursor()
    cur.execute(f"SELECT payload FROM {table} WHERE id = ?", (id_,))
    row = cur.fetchone()
    con.close()
    if not row:
        return None
    return json.loads(row[0])

def get_all(table: str) -> List[Dict[str, Any]]:
    con = connect()
    cur = con.cursor()
    cur.execute(f"SELECT payload FROM {table} ORDER BY id")
    rows = cur.fetchall()
    con.close()
    return [json.loads(r[0]) for r in rows]

def upsert(table: str, id_: str, payload: Dict[str, Any]) -> None:
    con = connect()
    cur = con.cursor()
    cur.execute(
        f"INSERT INTO {table} (id, payload) VALUES (?, ?) "
        "ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
        (id_, json.dumps(payload)),
    )
    con.commit()
    con.close()

def init_tables() -> None:
    con = connect()
    cur = con.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
    # Migrate run_events if it exists with the old single-key schema
    cur.execute("PRAGMA table_info(run_events)")
    existing_cols = {row[1] for row in cur.fetchall()}
    if existing_cols and "run_id" not in existing_cols:
        cur.execute("DROP TABLE run_events")
    cur.execute(
        "CREATE TABLE IF NOT EXISTS run_events (run_id TEXT NOT NULL, seq INTEGER NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(run_id, seq))"
    )
    con.commit()
    con.close()

def get_run(run_id: str) -> Optional[Dict[str, Any]]:
    con = connect()
    cur = con.cursor()
    cur.execute("SELECT payload FROM runs WHERE id = ?", (run_id,))
    row = cur.fetchone()
    con.close()
    return None if not row else json.loads(row[0])

def upsert_run(run_id: str, payload: Dict[str, Any]) -> None:
    con = connect()
    cur = con.cursor()
    cur.execute(
        "INSERT INTO runs (id, payload) VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
        (run_id, json.dumps(payload)),
    )
    con.commit()
    con.close()

def count_runs() -> int:
    con = connect()
    cur = con.cursor()
    cur.execute("SELECT COUNT(*) FROM runs")
    row = cur.fetchone()
    con.close()
    return row[0] if row else 0

def require_run_exists(run_id: str) -> Dict[str, Any]:
    r = get_run(run_id)
    if not r:
        raise HTTPException(status_code=404, detail="run not found")
    return r
def delete_run_events(run_id: str) -> None:
    con = connect()
    cur = con.cursor()
    cur.execute("DELETE FROM run_events WHERE run_id = ?", (run_id,))
    con.commit()
    con.close()

def insert_run_events(run_id: str, events: List[Dict[str, Any]]) -> None:
    con = connect()
    cur = con.cursor()
    for i, ev in enumerate(events):
        cur.execute(
            "INSERT OR REPLACE INTO run_events (run_id, seq, payload) VALUES (?, ?, ?)",
            (run_id, i, json.dumps(ev)),
        )
    con.commit()
    con.close()

def get_run_events(run_id: str) -> List[Dict[str, Any]]:
    con = connect()
    cur = con.cursor()
    cur.execute("SELECT payload FROM run_events WHERE run_id = ? ORDER BY seq ASC", (run_id,))
    rows = cur.fetchall()
    con.close()
    return [json.loads(r[0]) for r in rows]
