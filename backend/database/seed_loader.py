"""Seed Loader (Task 2)

One command populates a local SQLite database with deterministic demo data.
This intentionally stores payloads as JSON blobs so Layer-1 API can return
contract shapes immediately, even before full relational modeling exists.

Usage (from anywhere):
  python backend/seed_loader.py      # from project root
  python seed_loader.py              # from backend/
  cd backend && python seed_loader.py

Environment overrides (optional):
  SEED_DIR=backend/seed
  DB_PATH=backend/dev.db
"""

import json
import os
import sqlite3
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent

SEED_DIR = Path(os.getenv("SEED_DIR", _SCRIPT_DIR / "seed"))
DB_PATH = Path(os.getenv("DB_PATH", _SCRIPT_DIR / "dev.db"))

TABLES = [
  "connectors", "uploads", "runs", "run_events", "evidence", "entities",
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
  conn.commit()

def upsert(conn: sqlite3.Connection, table: str, id_: str, payload: dict) -> None:
  cur = conn.cursor()
  cur.execute(
    f"INSERT INTO {table} (id, payload) VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
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
  data = load_file(FILES["connectors"])
  for c in data:
    upsert(conn, "connectors", c["id"], c)

  # uploads
  data = load_file(FILES["uploads"])
  for u in data:
    upsert(conn, "uploads", u["id"], u)

  # run (single)
  r = load_file(FILES["runs"])
  upsert(conn, "runs", r["id"], r)

  # events
  ev = load_file(FILES["run_events"])
  for i, e in enumerate(ev, start=1):
    upsert(conn, "run_events", f"re_{i:03d}", e)

  # evidence
  data = load_file(FILES["evidence"])
  for e in data:
    upsert(conn, "evidence", e["id"], e)

  # entities
  data = load_file(FILES["entities"])
  for e in data:
    upsert(conn, "entities", e["id"], e)

  # mappings
  data = load_file(FILES["mappings"])
  for m in data:
    upsert(conn, "mappings", m["id"], m)

  # permissions
  data = load_file(FILES["permissions"])
  for p in data:
    upsert(conn, "permissions", p["id"], p)

  # opportunities
  data = load_file(FILES["opportunities"])
  for o in data:
    upsert(conn, "opportunities", o["id"], o)

  # audit
  data = load_file(FILES["audit_events"])
  for a in data:
    upsert(conn, "audit_events", a["id"], a)

  # executive report
  rep = load_file(FILES["executive_reports"])
  upsert(conn, "executive_reports", "exec_001", rep)

  # Verification checks (DoD)
  # - opp_002.effort=7, opp_005.effort=7, opp_006.decision=APPROVED, opp_008 impact=5 effort=2
  opps = load_file(FILES["opportunities"])
  idx = {o["id"]: o for o in opps}
  assert idx["opp_002"]["effort"] == 7
  assert idx["opp_005"]["effort"] == 7
  assert idx["opp_006"]["decision"] == "APPROVED"
  assert idx["opp_008"]["impact"] == 5 and idx["opp_008"]["effort"] == 2

  # Roadmap guard: ensure Stage 90 has at least one Complex opportunity in the selection pool.
  complex_unreviewed = [o for o in opps if o.get("tier") == "Complex" and o.get("decision") == "UNREVIEWED"]
  complex_approved = [o for o in opps if o.get("tier") == "Complex" and o.get("decision") == "APPROVED"]
  stage90_count = len(complex_unreviewed[:1] + complex_approved[:1])
  assert stage90_count >= 1, "Stage 90 would be empty: need at least one Complex opportunity UNREVIEWED or APPROVED"

  print("✅ Seed load complete:", DB_PATH)
  print("✅ Verified QA-critical opportunity values.")
  conn_count = len(load_file(FILES["connectors"]))
  upload_count = len(load_file(FILES["uploads"]))
  opp_count = len(opps)
  print(f"   {conn_count} connectors | {upload_count} uploads | {opp_count} opportunities")

if __name__ == "__main__":
  main()