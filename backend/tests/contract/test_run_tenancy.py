"""Contract tests — run-level multi-tenancy isolation.

A run created in org A must not be visible to org B. Enforced centrally:
  * upsert_run stamps the owning org_id from the request context on creation
    (preserving any org_id already on the payload).
  * require_run_exists denies cross-org reads as 404 — every run-scoped endpoint
    funnels through it via read_run / run_get, so one guard covers them all.

Background: before this, the frontend signed all data calls with the static dev
token (no org claim → everyone resolved to the `default` org) AND runs were
created without an org_id and read with no org filter, so any authenticated user
could read any run by ID across orgs.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

AUTH = {"Authorization": "Bearer dev-token-change-me"}


# ---------------------------------------------------------------------------
# require_run_exists — cross-org enforcement (unit)
# ---------------------------------------------------------------------------


def test_require_run_exists_denies_cross_org():
    """A run owned by org_B is 404 to an org_A request (silent deny)."""
    from app import db

    run = {"id": "r1", "org_id": "org_B"}
    with patch("app.db.get_run", return_value=run):
        with patch("app.db._current_request_org", return_value="org_A"):
            with pytest.raises(HTTPException) as exc:
                db.require_run_exists("r1")
    assert exc.value.status_code == 404


def test_require_run_exists_allows_same_org():
    from app import db

    run = {"id": "r1", "org_id": "org_A"}
    with patch("app.db.get_run", return_value=run):
        with patch("app.db._current_request_org", return_value="org_A"):
            assert db.require_run_exists("r1") == run


def test_require_run_exists_allows_legacy_untagged_run():
    """Runs created before isolation (no org_id) stay visible — no hard break."""
    from app import db

    run = {"id": "r1"}  # no org_id
    with patch("app.db.get_run", return_value=run):
        with patch("app.db._current_request_org", return_value="org_A"):
            assert db.require_run_exists("r1") == run


def test_require_run_exists_allows_when_no_request_context():
    """Background reads (no request context) are not filtered."""
    from app import db

    run = {"id": "r1", "org_id": "org_B"}
    with patch("app.db.get_run", return_value=run):
        with patch("app.db._current_request_org", return_value=None):
            assert db.require_run_exists("r1") == run


# ---------------------------------------------------------------------------
# upsert_run — org stamping (unit)
# ---------------------------------------------------------------------------


def test_upsert_run_stamps_org_from_request_context():
    from app import db

    with patch("app.db._current_request_org", return_value="org_stamp"):
        db.upsert_run("run_stamp_1", {"id": "run_stamp_1", "status": "running"})
    stored = db.get_run("run_stamp_1")
    assert stored["org_id"] == "org_stamp"


def test_upsert_run_preserves_existing_org_id():
    """Status updates / materialization read-modify-write must keep the owner."""
    from app import db

    with patch("app.db._current_request_org", return_value="org_other"):
        db.upsert_run("run_stamp_2", {"id": "run_stamp_2", "org_id": "org_owner"})
    stored = db.get_run("run_stamp_2")
    assert stored["org_id"] == "org_owner"  # context never overrides an existing tag


def test_upsert_run_no_context_leaves_untagged():
    from app import db

    with patch("app.db._current_request_org", return_value=None):
        db.upsert_run("run_stamp_3", {"id": "run_stamp_3"})
    stored = db.get_run("run_stamp_3")
    assert "org_id" not in stored


# ---------------------------------------------------------------------------
# End-to-end via the real route + tenancy middleware
# GET /api/runs/{run_id} requires only require_auth (no role gate), so the dev
# token plus DEV_JWT_ORG is enough to drive the org context per request.
# ---------------------------------------------------------------------------


def test_get_run_route_isolates_by_org(client: TestClient, monkeypatch):
    from app import db
    from app.rbac import seed_owner

    # Created with no request context → org_id passes through unchanged.
    db.upsert_run(
        "run_route_iso",
        {"id": "run_route_iso", "org_id": "org_alpha", "status": "done"},
    )

    # GET /api/runs/{run_id} is role-gated (csc rbac fix 60a84c3), so the dev user
    # needs a role in each org this test drives — otherwise the RBAC gate (403)
    # fires before the tenancy guard we are actually testing. Seed both orgs as
    # owner so RBAC passes and we assert the tenancy outcome (200 vs 404).
    seed_owner("org_alpha", "dev-token-change-me")
    seed_owner("org_beta", "dev-token-change-me")

    # Same org → visible.
    monkeypatch.setenv("DEV_JWT_ORG", "org_alpha")
    assert client.get("/api/runs/run_route_iso", headers=AUTH).status_code == 200

    # Different org → 404, indistinguishable from not-found.
    monkeypatch.setenv("DEV_JWT_ORG", "org_beta")
    assert client.get("/api/runs/run_route_iso", headers=AUTH).status_code == 404


def test_get_run_route_legacy_untagged_visible_any_org(client: TestClient, monkeypatch):
    from app import db
    from app.rbac import seed_owner

    with patch("app.db._current_request_org", return_value=None):
        db.upsert_run("run_route_legacy", {"id": "run_route_legacy", "status": "done"})

    # Role-gated route (60a84c3): seed the dev user in the querying org so the
    # RBAC gate passes and we assert the legacy-visibility (tenancy) behaviour.
    seed_owner("any_org_at_all", "dev-token-change-me")
    monkeypatch.setenv("DEV_JWT_ORG", "any_org_at_all")
    assert client.get("/api/runs/run_route_legacy", headers=AUTH).status_code == 200


# ---------------------------------------------------------------------------
# Run-scoped artifact endpoints must funnel through the run-ownership guard.
# GET /api/runs/{run_id}/connector-health reads run-scoped KV (keyed only by
# run_id), so without an explicit require_run_exists it would leak one org's
# connector health to another org that knows the run id (R17-D3 / AT-448).
# ---------------------------------------------------------------------------


def test_connector_health_route_isolates_by_org(client: TestClient, monkeypatch):
    from app import db
    from app.rbac import seed_owner

    # The connector-health route is viewer+; give the dev user a role in BOTH
    # orgs so the test exercises the tenancy guard (404), not the RBAC gate (403).
    seed_owner("org_ch_owner", "dev-token-change-me")
    seed_owner("org_ch_other", "dev-token-change-me")

    db.upsert_run(
        "run_conn_health_iso",
        {"id": "run_conn_health_iso", "org_id": "org_ch_owner", "status": "done"},
    )
    db.run_kv_set(
        "connector_health",
        "run_conn_health_iso",
        {"ServiceNow": {"system": "ServiceNow", "status": "live", "isLive": True}},
    )

    # Owning org can read the stored health.
    monkeypatch.setenv("DEV_JWT_ORG", "org_ch_owner")
    ok = client.get("/api/runs/run_conn_health_iso/connector-health", headers=AUTH)
    assert ok.status_code == 200
    assert ok.json()["ServiceNow"]["status"] == "live"

    # A different org is denied as 404 — never sees the owner's connector health.
    monkeypatch.setenv("DEV_JWT_ORG", "org_ch_other")
    denied = client.get("/api/runs/run_conn_health_iso/connector-health", headers=AUTH)
    assert denied.status_code == 404


# ---------------------------------------------------------------------------
# GET /api/runs — org-scoped list, newest-first
# Lets any org member (and the creator after logout) re-find the workspace's
# run instead of it living only in one browser's localStorage.
# ---------------------------------------------------------------------------


def test_list_runs_is_org_scoped_and_newest_first(client: TestClient):
    from app import db

    db.upsert_run(
        "run_list_a",
        {"id": "run_list_a", "org_id": "default", "status": "done",
         "startedAt": "2026-01-02T00:00:00Z"},
    )
    db.upsert_run(
        "run_list_b",
        {"id": "run_list_b", "org_id": "default", "status": "running",
         "startedAt": "2026-01-03T00:00:00Z"},
    )
    db.upsert_run(
        "run_list_other_org",
        {"id": "run_list_other_org", "org_id": "cf_org", "status": "done",
         "startedAt": "2026-01-09T00:00:00Z"},
    )

    resp = client.get("/api/runs", headers=AUTH)  # dev token → org "default"
    assert resp.status_code == 200
    ids = [r["id"] for r in resp.json()]

    assert "run_list_a" in ids and "run_list_b" in ids
    assert "run_list_other_org" not in ids, "another org's run must not be listed"
    # Newest-first by startedAt: b (Jan 3) precedes a (Jan 2).
    assert ids.index("run_list_b") < ids.index("run_list_a")
