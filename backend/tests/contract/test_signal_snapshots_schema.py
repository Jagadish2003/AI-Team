"""Schema lock test for signal_snapshots — PostgreSQL (AT-288 / Fix 1).

Ported from SQLite PRAGMA introspection to PostgreSQL information_schema /
pg_indexes. The conftest migrates the test database to head before the suite
runs, so this module introspects that live schema directly.
"""
import sqlite3  # routed to PostgreSQL by conftest (test-only helper)

import psycopg2
import pytest

# (name, data_type, character_maximum_length, required, primary_key)
EXPECTED_COLUMNS = [
    ("id", "character varying", 36, True, True),
    ("org_id", "character varying", 64, True, False),
    ("run_id", "character varying", 64, True, False),
    ("pack_id", "character varying", 64, True, False),
    ("detector_id", "character varying", 128, True, False),
    ("signal_key", "character varying", 256, True, False),
    ("metric_name", "character varying", 128, True, False),
    ("metric_value", "double precision", None, True, False),
    ("threshold", "double precision", None, False, False),
    ("fired", "boolean", None, True, False),
    ("signal_source", "character varying", 64, True, False),
    ("captured_at", "timestamp without time zone", None, True, False),
    ("baseline_mean", "double precision", None, False, False),
    ("baseline_stddev", "double precision", None, False, False),
    ("baseline_window_days", "integer", None, False, False),
    ("baseline_calculated_at", "timestamp without time zone", None, False, False),
]

EXPECTED_INDEXES = {
    "idx_ss_org_signal_time": ["org_id", "signal_key", "captured_at"],
    "idx_ss_org_run": ["org_id", "run_id"],
    "idx_ss_org_detector": ["org_id", "detector_id", "captured_at"],
    "idx_ss_baseline_stale": ["baseline_calculated_at"],
}


@pytest.fixture()
def conn():
    connection = sqlite3.connect("")  # conftest routes this to PostgreSQL
    try:
        yield connection
    finally:
        connection.close()


def _columns(conn):
    return conn.execute(
        """
        SELECT column_name, data_type, character_maximum_length, is_nullable
        FROM information_schema.columns
        WHERE table_name = 'signal_snapshots'
        ORDER BY ordinal_position
        """
    ).fetchall()


def _primary_key_columns(conn):
    rows = conn.execute(
        """
        SELECT a.attname
        FROM pg_index i
        JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
        WHERE i.indrelid = 'signal_snapshots'::regclass AND i.indisprimary
        """
    ).fetchall()
    return {r[0] for r in rows}


def test_signal_snapshots_schema_matches_locked_column_spec(conn):
    rows = _columns(conn)
    assert [r["column_name"] for r in rows] == [c[0] for c in EXPECTED_COLUMNS]

    pk_cols = _primary_key_columns(conn)
    for row, (name, data_type, char_len, required, primary_key) in zip(rows, EXPECTED_COLUMNS):
        assert row["column_name"] == name
        assert row["data_type"] == data_type
        assert row["character_maximum_length"] == char_len
        assert (row["is_nullable"] == "NO") is required
        assert (name in pk_cols) is primary_key


def test_signal_snapshots_required_indexes_exist(conn):
    index_defs = {
        r["indexname"]: r["indexdef"]
        for r in conn.execute(
            "SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'signal_snapshots'"
        ).fetchall()
    }
    assert set(EXPECTED_INDEXES).issubset(set(index_defs))

    for index_name, expected_columns in EXPECTED_INDEXES.items():
        indexdef = index_defs[index_name]
        # Every expected column must appear, in order, inside the index def.
        positions = [indexdef.find(col) for col in expected_columns]
        assert all(p >= 0 for p in positions), (index_name, indexdef)
        assert positions == sorted(positions), (index_name, indexdef)

    # captured_at must be DESC on the two time-range indexes.
    assert "captured_at DESC" in index_defs["idx_ss_org_signal_time"]
    assert "captured_at DESC" in index_defs["idx_ss_org_detector"]


def test_org_id_is_required(conn):
    with pytest.raises(psycopg2.IntegrityError):
        conn.execute(
            """
            INSERT INTO signal_snapshots (
                id, org_id, run_id, pack_id, detector_id, signal_key,
                metric_name, metric_value, threshold, fired, signal_source,
                captured_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
    conn.rollback()


def test_org_scoped_history_query_never_returns_other_org_rows(conn):
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
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        snapshots,
    )
    conn.commit()

    rows = conn.execute(
        """
        SELECT org_id, run_id, metric_value
        FROM signal_snapshots
        WHERE org_id = %s AND signal_key = %s
        ORDER BY captured_at DESC
        """,
        ("org_A", "service_cloud::application_stall::metric_value"),
    ).fetchall()

    assert [dict(row) for row in rows] == [
        {"org_id": "org_A", "run_id": "run_A", "metric_value": 9.0}
    ]
