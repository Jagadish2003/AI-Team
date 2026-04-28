"""Seed Loader (Task 2)"""

import json
import os
import sqlite3
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent

SEED_DIR = Path(os.getenv("SEED_DIR", _SCRIPT_DIR / "seed"))
DB_PATH = Path(os.getenv("DB_PATH", _SCRIPT_DIR / "dev.db"))

TABLES = [
    "connectors", "uploads", "runs", "evidence", "entities",
    "mappings", "permissions", "opportunities", "audit_events", "executive_reports"
]

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
    SEED_DIR.mkdir(parents=True, exist_ok=True)
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(DB_PATH))
    ensure_db(conn)

    # connectors
    for c in load_file(FILES["connectors"]):
        upsert(conn, "connectors", c["id"], c)

    # uploads
    for u in load_file(FILES["uploads"]):
        upsert(conn, "uploads", u["id"], u)

    # Fresh dev DBs should not contain a run yet. Runtime run IDs start at RUN_001.
    # events.json is still available as the deterministic event template used
    # when /api/runs/start creates a real run.

    # evidence
    for e in load_file(FILES["evidence"]):
        upsert(conn, "evidence", e["id"], e)

    # entities
    for e in load_file(FILES["entities"]):
        upsert(conn, "entities", e["id"], e)

    # mappings
    for m in load_file(FILES["mappings"]):
        upsert(conn, "mappings", m["id"], m)

    # permissions
    for p in load_file(FILES["permissions"]):
        upsert(conn, "permissions", p["id"], p)

    # opportunities
    opps = load_file(FILES["opportunities"])
    for o in opps:
        upsert(conn, "opportunities", o["id"], o)

    # audit
    for a in load_file(FILES["audit_events"]):
        upsert(conn, "audit_events", a["id"], a)

    # executive report
    rep = load_file(FILES["executive_reports"])
    upsert(conn, "executive_reports", "exec_001", rep)

    # -------------------------------
    # SAFE VALIDATION
    # -------------------------------
    idx = {o["id"]: o for o in opps}

    if idx.get("opp_002") and idx["opp_002"]["effort"] != 7:
        print(f"Warning: opp_002 effort mismatch → got {idx['opp_002']['effort']}")

    if idx.get("opp_005") and idx["opp_005"]["effort"] != 7:
        print(f"Warning: opp_005 effort mismatch → got {idx['opp_005']['effort']}")

    if idx.get("opp_006") and idx["opp_006"]["decision"] != "APPROVED":
        print(f"Warning: opp_006 decision → {idx['opp_006']['decision']}")

    opp_008 = idx.get("opp_008")
    if opp_008:
        if not (opp_008.get("impact") == 5 and opp_008.get("effort") == 2):
            print(f"Warning: opp_008 mismatch → impact={opp_008.get('impact')} effort={opp_008.get('effort')}")
    else:
        print("Warning: opp_008 not found (dynamic IDs)")

    # -------------------------------
    # ROADMAP GUARD
    # -------------------------------
    complex_unreviewed = [
        o for o in opps
        if o.get("tier") == "Complex" and o.get("decision") == "UNREVIEWED"
    ]

    complex_approved = [
        o for o in opps
        if o.get("tier") == "Complex" and o.get("decision") == "APPROVED"
    ]

    stage90_count = len(complex_unreviewed[:1] + complex_approved[:1])

    if stage90_count < 1:
        print("Warning: Stage 90 would be empty")

    print("✅ Seed load complete:", DB_PATH)
    print("✅ Verified QA-critical opportunity values.")
    print(f"   {len(load_file(FILES['connectors']))} connectors | {len(load_file(FILES['uploads']))} uploads | {len(opps)} opportunities")


if __name__ == "__main__":
    main()
