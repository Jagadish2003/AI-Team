"""Contract tests — RBAC (AT-82 T11).

AC6: Viewer cannot start a run → 403, run not created.
AC7: Analyst cannot view audit log → 403.
AC8: Owner can perform all actions.
     Unauthenticated requests return 401.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

AUTH = {"Authorization": "Bearer dev-token-change-me"}
DEV_USER = "dev-token-change-me"


# ---------------------------------------------------------------------------
# Fixtures — insert specific roles for the dev user in test orgs
# ---------------------------------------------------------------------------


def _set_role(role: str) -> dict:
    """Return headers that put the dev user in a freshly seeded org with given role."""
    import uuid
    from datetime import datetime, timezone

    from app import db
    from app.rbac import _ensure_members_table

    _ensure_members_table()
    org_id = f"rbac_test_{uuid.uuid4().hex[:8]}"
    con = db.connect()
    try:
        con.execute(
            "INSERT OR REPLACE INTO workspace_members (org_id, user_id, role, created_at) "
            "VALUES (?, ?, ?, ?)",
            (org_id, DEV_USER, role, datetime.now(timezone.utc).isoformat()),
        )
        con.commit()
    finally:
        con.close()
    return {**AUTH, "X-Org-Id": org_id}


# ---------------------------------------------------------------------------
# AC6 — viewer cannot start a run
# ---------------------------------------------------------------------------


def test_viewer_cannot_start_run(client: TestClient):
    """Viewer role → POST /api/runs/start returns 403 (AC6)."""
    headers = _set_role("viewer")
    resp = client.post(
        "/api/runs/start",
        json={"connectedSources": [], "uploadedFiles": [], "sampleWorkspaceEnabled": False,
              "mode": "offline", "systems": ["salesforce"]},
        headers=headers,
    )
    assert resp.status_code == 403
    assert resp.json()["detail"] == "Insufficient role"


def test_viewer_start_run_does_not_create_run(client: TestClient):
    """After a 403 on /api/runs/start, no run record must have been written (AC6)."""
    from app import db
    before_ids = {r.get("id") for r in db.get_all("runs")}

    headers = _set_role("viewer")
    client.post(
        "/api/runs/start",
        json={"connectedSources": [], "uploadedFiles": [], "sampleWorkspaceEnabled": False,
              "mode": "offline", "systems": ["salesforce"]},
        headers=headers,
    )

    after_ids = {r.get("id") for r in db.get_all("runs")}
    assert before_ids == after_ids, "No new run must be created when RBAC denies the request"


# ---------------------------------------------------------------------------
# AC7 — analyst cannot view audit log
# ---------------------------------------------------------------------------


def test_analyst_cannot_view_run_audit(client: TestClient):
    """Analyst role → GET /api/runs/{run_id}/audit returns 403 (AC7)."""
    headers = _set_role("analyst")
    resp = client.get("/api/runs/run_fake_id/audit", headers=headers)
    assert resp.status_code == 403
    assert resp.json()["detail"] == "Insufficient role"


def test_analyst_cannot_view_org_audit_log(client: TestClient):
    """Analyst role → GET /api/audit-log returns 403 (AC7)."""
    headers = _set_role("analyst")
    resp = client.get("/api/audit-log", headers=headers)
    assert resp.status_code == 403


def test_viewer_cannot_manage_connectors(client: TestClient):
    """Viewer role → POST /api/connectors/{id}/connect returns 403."""
    headers = _set_role("viewer")
    resp = client.post(
        "/api/connectors/salesforce/connect",
        json={"status": "connected"},
        headers=headers,
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# AC10 — GET /api/workspace/members: owner only
# ---------------------------------------------------------------------------


def test_analyst_cannot_view_workspace_members(client: TestClient):
    """Analyst role → GET /api/workspace/members returns 403 (AC10)."""
    headers = _set_role("analyst")
    resp = client.get("/api/workspace/members", headers=headers)
    assert resp.status_code == 403
    assert resp.json()["detail"] == "Insufficient role"


def test_viewer_cannot_view_workspace_members(client: TestClient):
    """Viewer role → GET /api/workspace/members returns 403 (AC10)."""
    headers = _set_role("viewer")
    resp = client.get("/api/workspace/members", headers=headers)
    assert resp.status_code == 403
    assert resp.json()["detail"] == "Insufficient role"


def test_owner_workspace_members_returns_list(client: TestClient):
    """Owner role → GET /api/workspace/members returns a list with role fields (AC10)."""
    resp = client.get("/api/workspace/members", headers=AUTH)
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)
    # Dev user seeded as owner of 'default' org in lifespan
    roles = {m["role"] for m in resp.json()}
    assert "owner" in roles


# ---------------------------------------------------------------------------
# AC8 — owner can perform all actions
# ---------------------------------------------------------------------------


def test_owner_can_start_run(client: TestClient):
    """Owner role → POST /api/runs/start succeeds (not 403)."""
    # Default org has dev-user as owner (seeded in lifespan)
    resp = client.post(
        "/api/runs/start",
        json={"connectedSources": [], "uploadedFiles": [], "sampleWorkspaceEnabled": False,
              "mode": "offline", "systems": ["salesforce"]},
        headers=AUTH,
    )
    # 200 or 422/500 (missing materialization fixtures) — never 403
    assert resp.status_code != 403


def test_owner_can_view_audit_log(client: TestClient):
    """Owner role → GET /api/audit-log does not return 403."""
    resp = client.get("/api/audit-log", headers=AUTH)
    assert resp.status_code != 403


def test_owner_can_manage_workspace_members(client: TestClient):
    """Owner role → GET /api/workspace/members does not return 403."""
    resp = client.get("/api/workspace/members", headers=AUTH)
    assert resp.status_code != 403


# ---------------------------------------------------------------------------
# Unauthenticated → 401
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method,path,body", [
    ("GET", "/api/connectors", None),
    ("POST", "/api/runs/start", {"connectedSources": [], "uploadedFiles": [],
                                  "sampleWorkspaceEnabled": False, "mode": "offline", "systems": []}),
    ("GET", "/api/audit-log", None),
    ("GET", "/api/workspace/members", None),
])
def test_unauthenticated_returns_401(client: TestClient, method, path, body):
    """All protected routes return 401 for unauthenticated requests."""
    if method == "GET":
        resp = client.get(path)
    else:
        resp = client.post(path, json=body)
    assert resp.status_code == 401
 