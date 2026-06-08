"""Seed Loader (Task 2) — Core data only (mock data excluded)"""

import json
import os
import sqlite3
import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent

SEED_DIR = Path(os.getenv("SEED_DIR", _SCRIPT_DIR / "seed"))
DB_PATH = Path(os.getenv("DB_PATH", _SCRIPT_DIR / "dev.db"))

print("Seed Path:", SEED_DIR)
print("DB Path:", DB_PATH)

TABLES = [
    "connectors", "uploads", "runs", "evidence",
    "mappings", "permissions", "opportunities", "audit_events", "executive_reports"
]
# NOTE: "entities" is intentionally excluded from TABLES. The entities table is
# now a 15-column Stage 2 schema managed by Alembic migration 0003. The old
# (id, payload) seed format is incompatible with the new schema. Stage 2 entity
# rows are written at run time by entity_extractor.py, not seeded.

FILES = {
    "connectors": "connectors.json",
    "uploads": "uploads.json",
    "runs": "run.json",
    "run_events": "events.json",
    "evidence": "evidence.json",
    "entities": "entities.json",
    "mappings": "mappings.json",
    "permissions": "permissions.json",
    "opportunities": "opportunities.json",
    "audit_events": "audit.json",
    "executive_reports": "executive_report.json",
}


def ensure_db(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()

    for t in TABLES:
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {t} (
                id TEXT PRIMARY KEY,
                payload TEXT NOT NULL
            )
        """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS run_events (
            run_id TEXT NOT NULL,
            seq INTEGER NOT NULL,
            payload TEXT NOT NULL,
            PRIMARY KEY (run_id, seq)
        )
    """)

    cur.execute("CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, payload TEXT NOT NULL)")
    conn.commit()


def upsert(conn: sqlite3.Connection, table: str, id_: str, payload: dict) -> None:
    cur = conn.cursor()
    cur.execute(
        f"INSERT INTO {table} (id, payload) VALUES (?, ?) "
        "ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
        (id_, json.dumps(payload))
    )
    conn.commit()


def load_file(name: str):
    p = SEED_DIR / name
    if not p.exists():
        raise SystemExit(f"Missing seed file: {p}")
    return json.loads(p.read_text(encoding="utf-8"))


def main():
    # Check for --fresh flag to delete existing database
    fresh = "--fresh" in sys.argv

    SEED_DIR.mkdir(parents=True, exist_ok=True)
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    # Delete existing database if --fresh flag is used
    if fresh and DB_PATH.exists():
        DB_PATH.unlink()
        print(f"🗑️  Deleted existing database: {DB_PATH}")

    conn = sqlite3.connect(str(DB_PATH))
    ensure_db(conn)

    # connectors
    for c in load_file(FILES["connectors"]):
        upsert(conn, "connectors", c["id"], c)

    # uploads
    for u in load_file(FILES["uploads"]):
        upsert(conn, "uploads", u["id"], u)

    # mappings
    for m in load_file(FILES["mappings"]):
        upsert(conn, "mappings", m["id"], m)

    # permissions
    for p in load_file(FILES["permissions"]):
        upsert(conn, "permissions", p["id"], p)

    # ─────────────────────────────────────────────────────────────────
    # MOCK DATA — Not loaded in dev.db (Sprint 9 cleanup)
    # ─────────────────────────────────────────────────────────────────
    # • evidence.json      — Demo discovery results
    # • opportunities.json — Demo roadmap data
    # • audit.json         — Demo audit events (see default_audit() in main.py)
    # • run.json           — Demo run config (runtime creates real run IDs)
    # • events.json        — Demo event template (not needed)
    # • executive_report.json — Demo report data
    #
    # These files remain in the seed/ folder as documentation/templates
    # but are not ingested into the database.

    # ────────────────────────────────────────────────────────────────────────
    # Load summary
    # ────────────────────────────────────────────────────────────────────────
    connectors_count = len(load_file(FILES["connectors"]))
    uploads_count = len(load_file(FILES["uploads"]))
    mappings_count = len(load_file(FILES["mappings"]))
    permissions_count = len(load_file(FILES["permissions"]))

    print("✅ Seed load complete:", DB_PATH)
    print(f"   {connectors_count} connectors | {mappings_count} mappings")
    print(f"   {permissions_count} permissions | {uploads_count} uploads")


if __name__ == "__main__":
    if "--help" in sys.argv or "-h" in sys.argv:
        print("""
Seed Loader — Populate dev.db with core data

Usage:
    python seed_loader.py              # Upsert data (idempotent, safe)
    python seed_loader.py --fresh      # Delete existing db and create new one

Flags:
    --fresh       Delete existing dev.db and create a fresh database
    --help, -h    Show this help message

Core Data Loaded:
    • connectors.json    (Integration Hub connectors)
    • mappings.json      (Process mappings)
    • permissions.json   (User permissions)
    • uploads.json       (File uploads)

Not Seeded:
    • entities — Stage 2 (15-column) schema is managed by Alembic migration
      0003 and populated at run time by entity_extractor.py. The legacy
      (id, payload) entities.json format is incompatible and is NOT loaded.
      See the TABLES note near the top of this file.

Mock Data Excluded:
    • opportunities.json, evidence.json, audit.json, run.json,
      events.json, executive_report.json
""")
        sys.exit(0)

    main()
