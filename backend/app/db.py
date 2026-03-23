import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

DB_PATH = Path(os.getenv("DB_PATH", "dev.db"))

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

def ensure_run_exists(run_id: str) -> None:
    run = get_one("runs", run_id)
    if not run:
        raise KeyError(f"run not found: {run_id}")
