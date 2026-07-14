"""Live-schema lock for the MSP-B8 staging schema — PostgreSQL (T1).

The conftest migrates the test database to head (including
0026_create_ops_event_staging.py) before the suite runs, so this module
introspects the live schema and exercises the constraints that carry the
acceptance criteria:

  * columns/types are locked (partner contract is stable),
  * UNIQUE (org_id, provider, provider_event_id) blocks duplicate re-loads (AC3),
  * an org-scoped ``row_id > checkpoint`` read is incremental and never crosses
    org boundaries (AC4 + AC6).
"""
import json

import psycopg2
import pytest

# (name, data_type, character_maximum_length, required, primary_key)
# event_time is last: added by ALTER (0027) so it appends after loaded_at, and the
# fresh-create path lists it last too — the migration path and a fresh create
# converge on the same column order.
EXPECTED_COLUMNS = [
    ("row_id", "bigint", None, True, True),
    ("org_id", "character varying", 64, True, False),
    ("provider", "character varying", 32, True, False),
    ("source_format", "character varying", 64, True, False),
    ("batch_id", "character varying", 128, True, False),
    ("provider_event_id", "character varying", 256, True, False),
    ("raw", "jsonb", None, True, False),
    ("loaded_at", "timestamp with time zone", None, True, False),
    ("event_time", "timestamp with time zone", None, False, False),
]

EXPECTED_INDEXES = {
    "idx_ops_event_staging_org_row": ["org_id", "row_id"],
    "idx_ops_event_staging_org_batch": ["org_id", "batch_id"],
    "idx_ops_event_staging_org_format": ["org_id", "provider", "source_format"],
}


@pytest.fixture()
def conn():
    import sqlite3  # conftest routes this to PostgreSQL

    connection = sqlite3.connect("")
    try:
        yield connection
    finally:
        connection.close()


def _columns(conn):
    return conn.execute(
        """
        SELECT column_name, data_type, character_maximum_length, is_nullable
        FROM information_schema.columns
        WHERE table_name = 'ops_event_staging'
        ORDER BY ordinal_position
        """
    ).fetchall()


def _primary_key_columns(conn):
    rows = conn.execute(
        """
        SELECT a.attname
        FROM pg_index i
        JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
        WHERE i.indrelid = 'ops_event_staging'::regclass AND i.indisprimary
        """
    ).fetchall()
    return {r[0] for r in rows}


def _insert(conn, org_id, provider, source_format, batch_id, provider_event_id, raw):
    conn.execute(
        """
        INSERT INTO ops_event_staging
            (org_id, provider, source_format, batch_id, provider_event_id, raw)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (org_id, provider, source_format, batch_id, provider_event_id, json.dumps(raw)),
    )


def test_ops_event_staging_columns_match_locked_spec(conn):
    rows = _columns(conn)
    assert [r["column_name"] for r in rows] == [c[0] for c in EXPECTED_COLUMNS]

    pk_cols = _primary_key_columns(conn)
    for row, (name, data_type, char_len, required, primary_key) in zip(rows, EXPECTED_COLUMNS):
        assert row["column_name"] == name
        assert row["data_type"] == data_type
        assert row["character_maximum_length"] == char_len
        assert (row["is_nullable"] == "NO") is required
        assert (name in pk_cols) is primary_key


def test_required_indexes_exist(conn):
    index_defs = {
        r["indexname"]: r["indexdef"]
        for r in conn.execute(
            "SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'ops_event_staging'"
        ).fetchall()
    }
    assert set(EXPECTED_INDEXES).issubset(set(index_defs))
    for index_name, expected_columns in EXPECTED_INDEXES.items():
        indexdef = index_defs[index_name]
        positions = [indexdef.find(col) for col in expected_columns]
        assert all(p >= 0 for p in positions), (index_name, indexdef)
        assert positions == sorted(positions), (index_name, indexdef)


def test_batch_registry_table_exists(conn):
    cols = [
        r["column_name"]
        for r in conn.execute(
            """
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'ops_event_load_batches' ORDER BY ordinal_position
            """
        ).fetchall()
    ]
    assert cols == [
        "org_id",
        "batch_id",
        "provider",
        "source_format",
        "source_reference",
        "record_count",
        "skipped_count",
        "loaded_at",
    ]


def test_org_id_and_raw_are_required(conn):
    with pytest.raises(psycopg2.IntegrityError):
        _insert(conn, None, "aws", "cloudtrail", "b1", "e1", {"k": "v"})
    conn.rollback()


def test_duplicate_provider_event_id_is_rejected(conn):
    """AC3: re-loading the same event (same provider_event_id) never duplicates."""
    _insert(conn, "org_dup", "aws", "cloudtrail", "batch1", "evt-1", {"eventID": "evt-1"})
    conn.commit()
    with pytest.raises(psycopg2.IntegrityError):
        # Same event id, even under a different batch, is rejected at the door.
        _insert(conn, "org_dup", "aws", "cloudtrail", "batch2", "evt-1", {"eventID": "evt-1"})
    conn.rollback()


def test_row_id_is_monotonic_and_incremental_read_is_org_scoped(conn):
    """AC4 + AC6: a row_id > checkpoint read returns only new rows, one org only."""
    for i in range(3):
        _insert(conn, "org_A", "aws", "cloudtrail", "bA", f"a-{i}", {"n": i})
    for i in range(2):
        _insert(conn, "org_B", "azure", "azure_activity_log", "bB", f"b-{i}", {"n": i})
    conn.commit()

    # First page for org_A from the beginning.
    first = conn.execute(
        """
        SELECT row_id, provider_event_id FROM ops_event_staging
        WHERE org_id = %s AND row_id > %s ORDER BY row_id ASC
        """,
        ("org_A", 0),
    ).fetchall()
    assert [r["provider_event_id"] for r in first] == ["a-0", "a-1", "a-2"]
    # Monotonically increasing.
    row_ids = [r["row_id"] for r in first]
    assert row_ids == sorted(row_ids)

    # A second run after the checkpoint sees nothing new — no org_B bleed-through.
    checkpoint = row_ids[-1]
    nothing_new = conn.execute(
        """
        SELECT row_id FROM ops_event_staging
        WHERE org_id = %s AND row_id > %s ORDER BY row_id ASC
        """,
        ("org_A", checkpoint),
    ).fetchall()
    assert nothing_new == []
