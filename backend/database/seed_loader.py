"""Seed Loader (Task 2) — Core data only (mock data excluded).

AT-288 / Fix 1: seeds into PostgreSQL via DATABASE_URL using native psycopg2.
The {id, payload} upsert SQL stays the same.
"""

import json
import os
import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
_BACKEND_DIR = _SCRIPT_DIR.parent

# Allow "python database/seed_loader.py" to import backend modules (the script's
# own dir is on sys.path, not backend/, so add backend/).
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import psycopg2  # noqa: E402
import psycopg2.extras  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

# Load backend/.env so DATABASE_URL (and SEED_DIR) are honoured without the
# caller having to export them. override=False — an exported value still wins.
load_dotenv(_BACKEND_DIR / ".env")

SEED_DIR = Path(os.getenv("SEED_DIR", _SCRIPT_DIR / "seed"))
DATABASE_URL = os.getenv("DATABASE_URL")

print("Seed Path:", SEED_DIR)
print("Database URL:", DATABASE_URL)

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


def ensure_db(conn) -> None:
    cur = conn.cursor()

    for t in TABLES:
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {t} (
                id TEXT PRIMARY KEY,
                payload TEXT NOT NULL
            )
        """)

    # runs needs the seq insertion-order column (see app/db.py init_tables).
    cur.execute("ALTER TABLE runs ADD COLUMN IF NOT EXISTS seq BIGSERIAL")

    cur.execute("""
        CREATE TABLE IF NOT EXISTS run_events (
            run_id TEXT NOT NULL,
            seq INTEGER NOT NULL,
            payload TEXT NOT NULL,
            is_deleted BOOLEAN NOT NULL DEFAULT FALSE,
            PRIMARY KEY (run_id, seq)
        )
    """)
    # Soft-delete column for DBs created before it existed (idempotent).
    cur.execute(
        "ALTER TABLE run_events ADD COLUMN IF NOT EXISTS is_deleted BOOLEAN NOT NULL DEFAULT FALSE"
    )

    cur.execute("CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, payload TEXT NOT NULL)")
    conn.commit()


# Full reset of the application's schema. PostgreSQL has no DB file to delete;
# the standard "fresh start" for one app is to drop every table and sequence in
# the `public` schema (this also removes alembic_version, so a subsequent
# `alembic upgrade head` re-runs all migrations from scratch). The dynamic drop
# only needs ownership of the objects (which the agentiq role has) — unlike
# `DROP SCHEMA public`, it does not require schema ownership, and unlike
# `DROP DATABASE` it needs no maintenance connection or CREATEDB privilege.
_RESET_PUBLIC_SCHEMA = """
DO $$
DECLARE r RECORD;
BEGIN
    FOR r IN (SELECT tablename FROM pg_tables WHERE schemaname = 'public') LOOP
        EXECUTE 'DROP TABLE IF EXISTS public.' || quote_ident(r.tablename) || ' CASCADE';
    END LOOP;
    FOR r IN (SELECT sequencename FROM pg_sequences WHERE schemaname = 'public') LOOP
        EXECUTE 'DROP SEQUENCE IF EXISTS public.' || quote_ident(r.sequencename) || ' CASCADE';
    END LOOP;
END $$;
"""


def reset_public_schema(conn) -> None:
    """Drop EVERY table + sequence in the public schema (full reset).

    Runs by default (skip with --no-reset). Includes the Alembic-managed tables
    and alembic_version, so run seed_loader BEFORE `alembic upgrade head` —
    running it after would wipe the migration tables Alembic just created.
    """
    cur = conn.cursor()
    cur.execute(_RESET_PUBLIC_SCHEMA)
    conn.commit()


def upsert(conn, table: str, id_: str, payload: dict) -> None:
    cur = conn.cursor()
    cur.execute(
        f"INSERT INTO {table} (id, payload) VALUES (%s, %s) "
        "ON CONFLICT (id) DO UPDATE SET payload=EXCLUDED.payload",
        (id_, json.dumps(payload))
    )
    conn.commit()


def load_file(name: str):
    p = SEED_DIR / name
    if not p.exists():
        raise SystemExit(f"Missing seed file: {p}")
    return json.loads(p.read_text(encoding="utf-8"))


def main():
    # By DEFAULT this performs a FULL reset of the public schema (the PostgreSQL
    # equivalent of deleting the SQLite file), then seeds. Run BEFORE
    # `alembic upgrade head`, which recreates the migration-managed tables.
    #
    # --no-reset skips the wipe and only ensures/seeds the core {id, payload}
    # tables. The test harness (tests/contract/conftest.py) uses it because it
    # manages its own reset + migration ordering and must NOT have the migration
    # tables dropped out from under it.
    skip_reset = "--no-reset" in sys.argv

    SEED_DIR.mkdir(parents=True, exist_ok=True)

    conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.DictCursor)

    if not skip_reset:
        reset_public_schema(conn)
        print("Reset: dropped all tables + sequences in the public schema")

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

    conn.close()
    print("Seed load complete:", DATABASE_URL)
    print(f"   {connectors_count} connectors | {mappings_count} mappings")
    print(f"   {permissions_count} permissions | {uploads_count} uploads")


if __name__ == "__main__":
    if "--help" in sys.argv or "-h" in sys.argv:
        print("""
Seed Loader — Populate the PostgreSQL agentiq DB with core data

Target DB: $DATABASE_URL

Fresh-database workflow (run from backend/, in this order):
    python database/seed_loader.py              # 1. wipe public schema + create/seed core tables
    alembic upgrade head                        # 2. create the migration-managed tables

WARNING: by default this DROPS every table + sequence in the public schema
(incl. alembic_version and the migration tables) before seeding — it is the
PostgreSQL equivalent of deleting the SQLite file. Run it BEFORE
`alembic upgrade head` (running it after would wipe the tables Alembic created).

Flags:
    --no-reset    Do NOT drop anything. Only create-if-missing and upsert the
                  core {id, payload} tables (idempotent, safe). Used by the test
                  harness, which manages its own reset/migration ordering.
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
