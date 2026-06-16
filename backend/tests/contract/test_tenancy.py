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

    # list_connectors now calls org_connectors_list — patch that in main's namespace
    with patch("app.main.org_connectors_list", side_effect=TenancyViolationError("no context")):
        resp = client.get("/api/connectors", headers=AUTH)

    assert resp.status_code == 500
    assert resp.json()["detail"] == "Tenancy context missing"


def test_middleware_sets_default_org(client: TestClient):
    """X-Org-Id header is not required; middleware defaults to 'default'."""
    resp = client.get("/api/health")
    assert resp.status_code == 200


def test_list_connectors_uses_tenancy_guard(client: TestClient):
    """GET /api/connectors goes through the org-scoped helper, not raw get_all (AC1)."""
    from unittest.mock import patch

    called_with: list[str] = []

    def _spy(org_id: str):
        called_with.append(org_id)
        return []

    # list_connectors must resolve the current org and call org_connectors_list
    # with it — never read connectors globally.
    with patch("app.main.org_connectors_list", side_effect=_spy):
        client.get("/api/connectors", headers=AUTH)

    assert called_with == ["default"], (
        "list_connectors must call org_connectors_list(<current org>), not raw get_all"
    )


def test_cross_org_connector_state_not_visible_via_route(client: TestClient):
    """One org connecting a connector must not change another org's view (AC1).

    Exercises the real per-org storage: connecting in another org writes a
    namespaced per-org row and must leave the default org's GET /api/connectors
    view of that connector exactly as it was. Order-independent: compares the
    default org's status before/after rather than assuming a clean fixture.
    """
    from app.db import org_connector_set, org_connectors_list

    def _sf_status():
        resp = client.get("/api/connectors", headers=AUTH)  # dev token → "default"
        assert resp.status_code == 200
        sf = next((c for c in resp.json() if c.get("id") == "salesforce"), None)
        return sf.get("status") if sf else None

    before = _sf_status()

    # A DIFFERENT org connects salesforce — writes only org_B_iso's namespaced row.
    org_connector_set(
        "org_B_iso",
        "salesforce",
        {"id": "salesforce", "name": "Salesforce", "status": "connected"},
    )

    assert _sf_status() == before, (
        "default org's connector view must be unaffected by another org connecting"
    )
    # And org_B_iso itself sees it connected — isolation, not suppression.
    assert any(
        c.get("id") == "salesforce" and c.get("status") == "connected"
        for c in org_connectors_list("org_B_iso")
    )


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
            "INSERT INTO workspace_members (org_id, user_id, role, created_at) "
            "VALUES (%s, %s, 'owner', %s) "
            "ON CONFLICT (org_id, user_id) DO UPDATE SET role=EXCLUDED.role, created_at=EXCLUDED.created_at",
            ("acme_corp", AUTH["Authorization"].split()[-1], datetime.now(timezone.utc).isoformat()),
        )
        con.commit()
    finally:
        con.close()

    captured: list[str] = []

    def _capture(*_args, **_kwargs):
        from app.middleware.tenancy import get_current_org_id
        captured.append(get_current_org_id())
        return []

    with patch("app.main.org_connectors_list", side_effect=_capture):
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

    def _capture(*_args, **_kwargs):
        from app.middleware.tenancy import get_current_org_id
        captured.append(get_current_org_id())
        return []

    from unittest.mock import patch
    with patch("app.main.org_connectors_list", side_effect=_capture):
        resp = client.get("/api/connectors", headers={**AUTH, "X-Org-Id": "default"})

    assert resp.status_code == 200
    assert captured and captured[0] == "default"


def test_no_jwt_claim_falls_back_to_x_org_id(client: TestClient, monkeypatch):
    """Backward-compat: with no JWT org claim, X-Org-Id still drives context (no 403)."""
    monkeypatch.delenv("DEV_JWT_ORG", raising=False)

    captured: list[str] = []

    def _capture(*_args, **_kwargs):
        from app.middleware.tenancy import get_current_org_id
        captured.append(get_current_org_id())
        return []

    from unittest.mock import patch
    with patch("app.main.org_connectors_list", side_effect=_capture):
        resp = client.get("/api/connectors", headers={**AUTH, "X-Org-Id": "acme_corp"})

    assert resp.status_code == 200
    assert captured and captured[0] == "acme_corp"


# ---------------------------------------------------------------------------
# AT-156 — Legacy table audit: tenancy_get_* wrappers (Section 4b)
# ---------------------------------------------------------------------------


def test_tenancy_get_connectors_filters_by_org(client: TestClient):
    """tenancy_get_connectors returns only rows tagged with the given org_id."""
    from app.db import tenancy_get_connectors, upsert

    upsert("connectors", "at156_conn_a", {"id": "at156_conn_a", "org_id": "at156_org_A"})
    upsert("connectors", "at156_conn_b", {"id": "at156_conn_b", "org_id": "at156_org_B"})

    result = tenancy_get_connectors("at156_org_A")
    ids = {c["id"] for c in result}
    assert "at156_conn_a" in ids
    assert "at156_conn_b" not in ids  # never cross-org
    assert all(c.get("org_id") == "at156_org_A" for c in result)


def test_tenancy_get_runs_filters_by_org(client: TestClient):
    """tenancy_get_runs returns only runs tagged with the given org_id."""
    from app.db import tenancy_get_runs, upsert_run

    upsert_run("at156_run_a", {"id": "at156_run_a", "org_id": "at156_org_A"})
    upsert_run("at156_run_b", {"id": "at156_run_b", "org_id": "at156_org_B"})

    result = tenancy_get_runs("at156_org_A")
    ids = {r["id"] for r in result}
    assert "at156_run_a" in ids
    assert "at156_run_b" not in ids  # never cross-org


def test_signal_snapshots_queries_filter_by_org(client: TestClient):
    """temporal.py signal_snapshots reads are scoped to org_id (AT-156 verify)."""
    from app.db import connect
    from app.temporal import get_run_signals, get_signal_history

    rows = [
        # (id, org_id, run_id, detector_id, signal_key, metric_name)
        ("ss_a", "ss_org_A", "ss_run", "DET", "DET::m", "m"),
        ("ss_b", "ss_org_B", "ss_run", "DET", "DET::m", "m"),
    ]
    con = connect()
    try:
        for _id, org, run_id, det, key, metric in rows:
            con.execute(
                "INSERT INTO signal_snapshots "
                "(id, org_id, run_id, pack_id, detector_id, signal_key, metric_name, "
                " metric_value, fired, signal_source, captured_at) "
                "VALUES (%s, %s, %s, 'pack', %s, %s, %s, 1.0, FALSE, 'offline', '2026-01-01T00:00:00Z')",
                (_id, org, run_id, det, key, metric),
            )
        con.commit()
    finally:
        con.close()

    # get_run_signals: only org_A's row for the shared run_id
    run_signals = get_run_signals(org_id="ss_org_A", run_id="ss_run")
    assert {r["id"] for r in run_signals} == {"ss_a"}

    # get_signal_history: only org_A's history
    history = get_signal_history(org_id="ss_org_A", detector_id="DET", signal_key="DET::m")
    assert all(r["org_id"] == "ss_org_A" for r in history)
    assert any(r["id"] == "ss_a" for r in history)
 