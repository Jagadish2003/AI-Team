"""RBAC role loader — DEV / DEMO ONLY.

Seeds workspace_members rows so the three role-bearing dev tokens can be
exercised against the running API (e.g. a Postman walkthrough of the RBAC
enforcement layer). All rows live in the 'default' org; you switch role by
switching the Bearer token, not by changing X-Org-Id.

    Token                  Role      Recognised by require_auth via
    ---------------------  --------  ------------------------------
    dev-token-change-me    owner     DEV_JWT     (default)
    viewer-token           viewer    VIEWER_JWT  (default)
    analyst-token          analyst   ANALYST_JWT (must be set in env)

Run from the backend/ directory:

    python database/rbac_loader.py

IMPORTANT — analyst token: require_auth only accepts the analyst token if the
SAME value is exported as ANALYST_JWT when the server starts, e.g.

    $env:ANALYST_JWT = "analyst-token"   # PowerShell, before uvicorn

DO NOT run this against a production database. Real deployments derive roles
from real workspace membership managed through the owner-only
/api/workspace/members flow; the static dev tokens must not carry roles there.
The legitimate owner-of-'default' row is also seeded automatically at app
startup (seed_owner() in app.main lifespan) — this loader just makes the full
owner/analyst/viewer set available for offline testing.
"""

import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent       # backend/database
_BACKEND_DIR = _SCRIPT_DIR.parent                    # backend

# Allow "python database/rbac_loader.py" to import the shared schema constant
# (the script's own dir is on sys.path, not backend/, so add backend/).
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

# Single source of truth for the table DDL — never re-declare it here.
from database.models.workspace_members import CREATE_WORKSPACE_MEMBERS_TABLE

# Match seed_loader.py's DB path resolution exactly.
DB_PATH = Path(os.getenv("DB_PATH", _SCRIPT_DIR / "dev.db"))

ORG_ID = "default"

# Token values mirror security.py (env-overridable, same defaults).
ROLES = [
    ("owner", os.getenv("DEV_JWT", "dev-token-change-me")),
    ("analyst", os.getenv("ANALYST_JWT", "analyst-token")),
    ("viewer", os.getenv("VIEWER_JWT", "viewer-token")),
]


def main() -> None:
    print("DB Path:", DB_PATH)
    now = datetime.now(timezone.utc).isoformat()

    conn = sqlite3.connect(str(DB_PATH))
    try:
        conn.execute(CREATE_WORKSPACE_MEMBERS_TABLE)
        for role, user_id in ROLES:
            conn.execute(
                "INSERT OR REPLACE INTO workspace_members "
                "(org_id, user_id, role, created_at) VALUES (?, ?, ?, ?)",
                (ORG_ID, user_id, role, now),
            )
        conn.commit()
    finally:
        conn.close()

    print("RBAC role seed complete (org='{}'):".format(ORG_ID))
    for role, user_id in ROLES:
        print("   {:<8} <- {}".format(role, user_id))
    print(
        "\nReminder: start the server with ANALYST_JWT set to the analyst token "
        "above so require_auth recognises it, e.g.\n"
        "   $env:ANALYST_JWT = \"{}\"".format(
            os.getenv("ANALYST_JWT", "analyst-token")
        )
    )


if __name__ == "__main__":
    main()
