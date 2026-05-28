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

    # main.py imports get_all directly; patch the name in main's namespace
    with patch("app.main.get_all", side_effect=TenancyViolationError("no context")):
        resp = client.get("/api/connectors", headers=AUTH)

    assert resp.status_code == 500
    assert resp.json()["detail"] == "Tenancy context missing"


def test_middleware_sets_default_org(client: TestClient):
    """X-Org-Id header is not required; middleware defaults to 'default'."""
    resp = client.get("/api/health")
    assert resp.status_code == 200


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

    with patch("app.main.get_all", side_effect=_capture):
        client.get("/api/connectors", headers={**AUTH, "X-Org-Id": "acme_corp"})

    assert captured and captured[0] == "acme_corp"
