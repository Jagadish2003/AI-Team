import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest


BACKEND_DIR = Path(__file__).resolve().parents[2]

EXPECTED_COLUMNS = [
    ("id", "VARCHAR(36)", True, True),
    ("org_id", "VARCHAR(64)", True, False),
    ("run_id", "VARCHAR(64)", True, False),
    ("pack_id", "VARCHAR(64)", True, False),
    ("detector_id", "VARCHAR(128)", True, False),
    ("signal_key", "VARCHAR(256)", True, False),
    ("metric_name", "VARCHAR(128)", True, False),
    ("metric_value", "DOUBLE", True, False),
    ("threshold", "DOUBLE", False, False),
    ("fired", "BOOLEAN", True, False),
    ("signal_source", "VARCHAR(64)", True, False),
    ("captured_at", "TIMESTAMP", True, False),
    ("baseline_mean", "DOUBLE", False, False),
    ("baseline_stddev", "DOUBLE", False, False),
    ("baseline_window_days", "INTEGER", False, False),
    ("baseline_calculated_at", "TIMESTAMP", False, False),
]

EXPECTED_INDEXES = {
    "idx_ss_org_signal_time": ["org_id", "signal_key", "captured_at"],
    "idx_ss_org_run": ["org_id", "run_id"],
    "idx_ss_org_detector": ["org_id", "detector_id", "captured_at"],
    "idx_ss_baseline_stale": ["baseline_calculated_at"],
}


@pytest.fixture(scope="module")
def migrated_db_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    db_path = tmp_path_factory.mktemp("signal_snapshots") / "temporal.db"
    env = {
        **os.environ,
        "DB_PATH": str(db_path),
        "PYTHONIOENCODING": "utf-8",
    }
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "alembic.ini", "upgrade", "head"],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr or result.stdout
    return db_path


@pytest.fixture()
def conn(migrated_db_path: Path):
    connection = sqlite3.connect(str(migrated_db_path))
    connection.row_factory = sqlite3.Row
    try:
        yield connection
    finally:
        connection.close()


def test_signal_snapshots_schema_matches_locked_column_spec(conn: sqlite3.Connection):
    rows = conn.execute("PRAGMA table_info(signal_snapshots)").fetchall()

    assert [row["name"] for row in rows] == [column[0] for column in EXPECTED_COLUMNS]

    for row, (name, expected_type, required, primary_key) in zip(rows, EXPECTED_COLUMNS):
        assert row["name"] == name
        assert row["type"].upper() == expected_type
        assert bool(row["notnull"]) is required
        assert bool(row["pk"]) is primary_key


def test_signal_snapshots_required_indexes_exist(conn: sqlite3.Connection):
    index_rows = conn.execute("PRAGMA index_list(signal_snapshots)").fetchall()
    index_names = {row["name"] for row in index_rows}

    assert set(EXPECTED_INDEXES).issubset(index_names)

    for index_name, expected_columns in EXPECTED_INDEXES.items():
        columns = [
            row["name"]
            for row in conn.execute(f"PRAGMA index_xinfo({index_name})").fetchall()
            if row["key"] and row["name"] is not None
        ]
        assert columns == expected_columns

    signal_time_index = conn.execute(
        "PRAGMA index_xinfo(idx_ss_org_signal_time)"
    ).fetchall()
    captured_at = next(row for row in signal_time_index if row["name"] == "captured_at")
    assert captured_at["desc"] == 1

    detector_index = conn.execute("PRAGMA index_xinfo(idx_ss_org_detector)").fetchall()
    detector_captured_at = next(
        row for row in detector_index if row["name"] == "captured_at"
    )
    assert detector_captured_at["desc"] == 1


def test_org_id_is_required(conn: sqlite3.Connection):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO signal_snapshots (
                id, org_id, run_id, pack_id, detector_id, signal_key,
                metric_name, metric_value, threshold, fired, signal_source,
                captured_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "11111111-1111-1111-1111-111111111111",
                None,
                "run_A",
                "service_cloud",
                "application_stall",
                "service_cloud::application_stall::metric_value",
                "metric_value",
                7.5,
                5.0,
                True,
                "salesforce",
                "2026-05-27T10:00:00+00:00",
            ),
        )


def test_org_scoped_history_query_never_returns_other_org_rows(
    conn: sqlite3.Connection,
):
    snapshots = [
        (
            "22222222-2222-2222-2222-222222222222",
            "org_A",
            "run_A",
            "service_cloud",
            "application_stall",
            "service_cloud::application_stall::metric_value",
            "metric_value",
            9.0,
            5.0,
            True,
            "salesforce",
            "2026-05-27T11:00:00+00:00",
        ),
        (
            "33333333-3333-3333-3333-333333333333",
            "org_B",
            "run_B",
            "service_cloud",
            "application_stall",
            "service_cloud::application_stall::metric_value",
            "metric_value",
            3.0,
            5.0,
            False,
            "salesforce",
            "2026-05-27T12:00:00+00:00",
        ),
    ]
    conn.executemany(
        """
        INSERT INTO signal_snapshots (
            id, org_id, run_id, pack_id, detector_id, signal_key,
            metric_name, metric_value, threshold, fired, signal_source,
            captured_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        snapshots,
    )

    rows = conn.execute(
        """
        SELECT org_id, run_id, metric_value
        FROM signal_snapshots
        WHERE org_id = ? AND signal_key = ?
        ORDER BY captured_at DESC
        """,
        ("org_A", "service_cloud::application_stall::metric_value"),
    ).fetchall()

    assert [dict(row) for row in rows] == [
        {"org_id": "org_A", "run_id": "run_A", "metric_value": 9.0}
    ]

    plan_rows = conn.execute(
        """
        EXPLAIN QUERY PLAN
        SELECT org_id, run_id, metric_value
        FROM signal_snapshots
        WHERE org_id = ? AND signal_key = ?
        ORDER BY captured_at DESC
        """,
        ("org_A", "service_cloud::application_stall::metric_value"),
    ).fetchall()
    assert any("idx_ss_org_signal_time" in row["detail"] for row in plan_rows)
