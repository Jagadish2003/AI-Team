import os
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

os.environ.setdefault("DEV_JWT", "dev-token-change-me")

from app.db import connect
from app.main import app
from app.routes_temporal import TEMPORAL_ROUTE_PATHS, register_temporal_routes


client = TestClient(app)


@pytest.fixture(autouse=True, scope="module")
def seed_roles():
    """Seed dev-token as owner and viewer-token as viewer of 'default' org."""
    from app.rbac import _ensure_members_table, seed_owner
    from app import db
    from datetime import datetime, timezone
    _ensure_members_table()
    seed_owner("default", "dev-token-change-me")
    con = db.connect()
    try:
        con.execute(
            "INSERT OR REPLACE INTO workspace_members (org_id, user_id, role, created_at) "
            "VALUES (?, ?, ?, ?)",
            ("default", "viewer-token", "viewer", datetime.now(timezone.utc).isoformat()),
        )
        con.commit()
    finally:
        con.close()


def auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {os.environ['DEV_JWT']}"}


def viewer_headers() -> dict[str, str]:
    return {"Authorization": "Bearer viewer-token"}


def insert_snapshot(
    *,
    org_id: str,
    detector_id: str,
    run_id: str,
    value: float,
    captured_at: str,
    baseline_mean=None,
    baseline_stddev=None,
    baseline_window_days=None,
    baseline_calculated_at=None,
) -> None:
    signal_key = f"pack::{detector_id}::metric_value"
    con = connect()
    try:
        con.execute(
            """
            INSERT INTO signal_snapshots (
                id, org_id, run_id, pack_id, detector_id, signal_key,
                metric_name, metric_value, threshold, fired, signal_source,
                captured_at, baseline_mean, baseline_stddev,
                baseline_window_days, baseline_calculated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid4()),
                org_id,
                run_id,
                "pack",
                detector_id,
                signal_key,
                "metric_value",
                value,
                None,
                True,
                "test",
                captured_at,
                baseline_mean,
                baseline_stddev,
                baseline_window_days,
                baseline_calculated_at,
            ),
        )
        con.commit()
    finally:
        con.close()


def test_temporal_route_registration_is_idempotent():
    test_app = FastAPI()

    register_temporal_routes(test_app)
    register_temporal_routes(test_app)

    route_paths = [
        getattr(route, "path", None)
        for route in test_app.routes
        if getattr(route, "path", None) in TEMPORAL_ROUTE_PATHS
    ]
    assert len(route_paths) == len(TEMPORAL_ROUTE_PATHS)


def test_history_route_defaults_to_metric_value_and_scopes_org():
    # Use "default" org — dev-token is seeded as owner there by seed_roles fixture.
    detector_id = f"det_task10_{uuid4().hex[:8]}"

    insert_snapshot(
        org_id="default",
        detector_id=detector_id,
        run_id="run_history_1",
        value=1.0,
        captured_at="2026-05-01T10:00:00",
    )
    insert_snapshot(
        org_id="default",
        detector_id=detector_id,
        run_id="run_history_2",
        value=2.0,
        captured_at="2026-05-02T10:00:00",
    )
    insert_snapshot(
        org_id="default",
        detector_id=detector_id,
        run_id="run_history_3",
        value=3.0,
        captured_at="2026-05-03T10:00:00",
    )

    # No X-Org-Id header → tenancy middleware defaults to "default" org.
    response = client.get(
        f"/api/temporal/{detector_id}/history",
        headers=auth_headers(),
        params={"limit": 2},
    )

    assert response.status_code == 200
    rows = response.json()
    assert [row["metric_value"] for row in rows] == [3.0, 2.0]
    assert {row["org_id"] for row in rows} == {"default"}


def test_baseline_route_returns_task10_shape_with_insufficient_data():
    # Use "default" org — dev-token is seeded as owner there by seed_roles fixture.
    detector_id = f"det_task10_{uuid4().hex[:8]}"

    insert_snapshot(
        org_id="default",
        detector_id=detector_id,
        run_id="run_baseline_1",
        value=10.0,
        captured_at="2026-05-01T10:00:00",
    )
    insert_snapshot(
        org_id="default",
        detector_id=detector_id,
        run_id="run_baseline_2",
        value=12.0,
        captured_at="2026-05-02T10:00:00",
    )

    response = client.get(
        f"/api/temporal/{detector_id}/baseline",
        headers=auth_headers(),
    )

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "baseline_mean",
        "baseline_stddev",
        "baseline_window_days",
        "calculated_at",
        "run_count",
        "insufficient_data",
    }
    assert body["run_count"] == 2
    assert body["insufficient_data"] is True


def test_run_signals_route_returns_404_for_different_org():
    # Data inserted for a non-"default" org; querying "default" org → 404.
    run_id = f"run_task10_{uuid4().hex[:8]}"
    detector_id = f"det_task10_{uuid4().hex[:8]}"

    insert_snapshot(
        org_id="org_task10_owner",
        detector_id=detector_id,
        run_id=run_id,
        value=7.0,
        captured_at="2026-05-01T10:00:00",
    )

    # No X-Org-Id → tenancy uses "default". run_id belongs to "org_task10_owner" → 404.
    response = client.get(
        f"/api/runs/{run_id}/signals",
        headers=auth_headers(),
    )

    assert response.status_code == 404


def test_temporal_routes_require_authentication():
    assert client.get("/api/temporal/det/history").status_code == 401
    assert client.get("/api/temporal/det/baseline").status_code == 401
    assert client.get("/api/runs/run_missing/signals").status_code == 401


def test_temporal_routes_forbid_viewer_role():
    assert (
        client.get("/api/temporal/det/history", headers=viewer_headers()).status_code
        == 403
    )
    assert (
        client.get("/api/temporal/det/baseline", headers=viewer_headers()).status_code
        == 403
    )
    assert (
        client.get("/api/runs/run_missing/signals", headers=viewer_headers()).status_code
        == 403
    )
