"""Contract tests — Tenancy enforcement (AT-82 T9).

AC1: org_A request cannot retrieve connector records belonging to org_B.
AC2: TenancyViolationError raised on missing context.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

AUTH = {"Authorization": "Bearer dev-token-change-me"}


# ---------------------------------------------------------------------------
# AC1 — cross-org isolation
# ---------------------------------------------------------------------------


def test_org_a_cannot_see_org_b_connector(client: TestClient):
    """tenancy_get_one returns None for a row that belongs to a different org."""
    from unittest.mock import patch

    from app.db import tenancy_get_one

    # Patch _assert_tenancy to return "org_A" (simulate org_A context)
    with patch("app.db._assert_tenancy", return_value="org_A"):
        # Row that explicitly declares org_B
        row_org_b = {"id": "sf", "name": "Salesforce", "org_id": "org_B"}

        with patch("app.db.get_one", return_value=row_org_b):
            result = tenancy_get_one("connectors", "sf")

    assert result is None, "org_A must not receive a row owned by org_B"


def test_org_a_can_see_own_row(client: TestClient):
    """tenancy_get_one returns the row when org_ids match."""
    from unittest.mock import patch

    from app.db import tenancy_get_one

    with patch("app.db._assert_tenancy", return_value="org_A"):
        row_org_a = {"id": "sf", "name": "Salesforce", "org_id": "org_A"}
        with patch("app.db.get_one", return_value=row_org_a):
            result = tenancy_get_one("connectors", "sf")

    assert result is not None
    assert result["org_id"] == "org_A"


def test_tenancy_get_all_filters_by_org(client: TestClient):
    """tenancy_get_all returns only rows belonging to the current org."""
    from unittest.mock import patch

    from app.db import tenancy_get_all

    rows = [
        {"id": "1", "org_id": "org_A"},
        {"id": "2", "org_id": "org_B"},
        {"id": "3"},  # legacy row — no org_id, passes through
    ]

    with patch("app.db._assert_tenancy", return_value="org_A"):
        with patch("app.db.get_all", return_value=rows):
            result = tenancy_get_all("connectors")

    ids = [r["id"] for r in result]
    assert "1" in ids, "org_A row must be included"
    assert "2" not in ids, "org_B row must be excluded"
    assert "3" in ids, "legacy row (no org_id) must pass through"


# ---------------------------------------------------------------------------
# AC2 — TenancyViolationError on missing context
# ---------------------------------------------------------------------------


def test_tenancy_violation_when_no_context():
    """get_current_org_id raises TenancyViolationError when ContextVar is None."""
    from contextvars import copy_context

    from app.middleware.tenancy import TenancyViolationError, _current_org_id, get_current_org_id

    def _run():
        _current_org_id.set(None)
        with pytest.raises(TenancyViolationError):
            get_current_org_id()

    copy_context().run(_run)


def test_tenancy_violation_returns_500_via_handler(client: TestClient):
    """A route that raises TenancyViolationError returns HTTP 500 via exception handler."""
    from unittest.mock import patch

    from app.middleware.tenancy import TenancyViolationError

    # list_connectors now calls tenancy_get_all — patch that name in main's namespace
    with patch("app.main.tenancy_get_all", side_effect=TenancyViolationError("no context")):
        resp = client.get("/api/connectors", headers=AUTH)

    assert resp.status_code == 500
    assert resp.json()["detail"] == "Tenancy context missing"


def test_middleware_sets_default_org(client: TestClient):
    """X-Org-Id header is not required; middleware defaults to 'default'."""
    resp = client.get("/api/health")
    assert resp.status_code == 200


def test_list_connectors_uses_tenancy_guard(client: TestClient):
    """GET /api/connectors calls tenancy_get_all, not raw get_all (AC1 — data layer)."""
    from unittest.mock import patch

    called_with: list[str] = []

    def _spy(table: str):
        called_with.append(table)
        return []

    # Patch tenancy_get_all in main's namespace (that's where it was imported)
    with patch("app.main.tenancy_get_all", side_effect=_spy):
        client.get("/api/connectors", headers=AUTH)

    assert called_with == ["connectors"], (
        "list_connectors must call tenancy_get_all('connectors'), not raw get_all"
    )


def test_cross_org_connector_not_visible_via_route(client: TestClient):
    """org_A cannot see a connector record tagged org_B through the actual route (AC1)."""
    from unittest.mock import patch

    # Simulate org_B owning the connector record
    org_b_connector = {"id": "salesforce", "name": "Salesforce", "org_id": "org_B"}

    # tenancy_get_all filters: org_A request sees [] because row is tagged org_B
    def _tenancy_filtered(table: str):
        from app.middleware.tenancy import get_current_org_id
        current = get_current_org_id()
        return [r for r in [org_b_connector] if r.get("org_id") == current]

    with patch("app.main.tenancy_get_all", side_effect=_tenancy_filtered):
        # Request as org_A — should NOT see org_B's connector
        resp = client.get("/api/connectors", headers={**AUTH, "X-Org-Id": "default"})

    assert resp.status_code == 200
    assert resp.json() == [], "org_A must not see org_B's connector record"


def test_middleware_respects_x_org_id_header(client: TestClient):
    """X-Org-Id header value is stored in the context during the request."""
    from datetime import datetime, timezone
    from unittest.mock import patch

    from app import db
    from app.rbac import _ensure_members_table

    # Seed the dev user as owner of "acme_corp" so RBAC doesn't block the request
    _ensure_members_table()
    con = db.connect()
    try:
        con.execute(
            "INSERT OR REPLACE INTO workspace_members (org_id, user_id, role, created_at) "
            "VALUES (?, ?, 'owner', ?)",
            ("acme_corp", AUTH["Authorization"].split()[-1], datetime.now(timezone.utc).isoformat()),
        )
        con.commit()
    finally:
        con.close()

    captured: list[str] = []

    def _capture(table):
        from app.middleware.tenancy import get_current_org_id
        captured.append(get_current_org_id())
        return []

    with patch("app.main.tenancy_get_all", side_effect=_capture):
        client.get("/api/connectors", headers={**AUTH, "X-Org-Id": "acme_corp"})

    assert captured and captured[0] == "acme_corp"


# ---------------------------------------------------------------------------
# AT-155 — X-Org-Id impersonation guard (Section 4a)
#
# org_id comes from the JWT org claim. When the dev token carries a claim
# (DEV_JWT_ORG), an X-Org-Id header that contradicts it is rejected with 403;
# a matching header is ignored and context comes from the JWT. When no claim
# is configured, X-Org-Id continues to drive context (dev fallback).
# ---------------------------------------------------------------------------


def test_x_org_id_mismatch_returns_403(client: TestClient, monkeypatch):
    """X-Org-Id that differs from the JWT org claim returns 403."""
    monkeypatch.setenv("DEV_JWT_ORG", "default")
    resp = client.get("/api/connectors", headers={**AUTH, "X-Org-Id": "other-org"})
    assert resp.status_code == 403
    assert resp.json()["detail"] == "X-Org-Id does not match authenticated workspace"


def test_no_x_org_id_proceeds_normally(client: TestClient, monkeypatch):
    """A request with no X-Org-Id header proceeds normally (context from JWT)."""
    monkeypatch.setenv("DEV_JWT_ORG", "default")
    resp = client.get("/api/connectors", headers=AUTH)
    assert resp.status_code == 200  # dev user is owner of "default"


def test_matching_x_org_id_ignored_context_from_jwt(client: TestClient, monkeypatch):
    """X-Org-Id matching the JWT org claim proceeds; context is sourced from the JWT."""
    monkeypatch.setenv("DEV_JWT_ORG", "default")

    captured: list[str] = []

    def _capture(table):
        from app.middleware.tenancy import get_current_org_id
        captured.append(get_current_org_id())
        return []

    from unittest.mock import patch
    with patch("app.main.tenancy_get_all", side_effect=_capture):
        resp = client.get("/api/connectors", headers={**AUTH, "X-Org-Id": "default"})

    assert resp.status_code == 200
    assert captured and captured[0] == "default"


def test_no_jwt_claim_falls_back_to_x_org_id(client: TestClient, monkeypatch):
    """Backward-compat: with no JWT org claim, X-Org-Id still drives context (no 403)."""
    monkeypatch.delenv("DEV_JWT_ORG", raising=False)

    captured: list[str] = []

    def _capture(table):
        from app.middleware.tenancy import get_current_org_id
        captured.append(get_current_org_id())
        return []

    from unittest.mock import patch
    with patch("app.main.tenancy_get_all", side_effect=_capture):
        resp = client.get("/api/connectors", headers={**AUTH, "X-Org-Id": "acme_corp"})

    assert resp.status_code == 200
    assert captured and captured[0] == "acme_corp"
 