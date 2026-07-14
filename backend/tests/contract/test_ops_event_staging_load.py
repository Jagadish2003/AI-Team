"""Real-DB idempotency for the AWS loaders → staging — MSP-B8 / T2.

The offline discovery tests prove the loaders' parsing / skip / dedupe logic
against an in-memory sink. This contract test proves the SAME idempotency holds
against the real ``ops_event_staging`` table via :class:`DbStagingSink` — the
``UNIQUE (org_id, provider, provider_event_id)`` constraint plus
``ON CONFLICT DO NOTHING`` (MSP-B8 AC3), and org isolation (AC6).

The conftest migrates the test database to head (including
0026_create_ops_event_staging.py) before the suite runs.
"""
import json
import os

import pytest

from discovery.ingest.aws_export_loaders import load_cloudtrail_logs
from discovery.ingest.ops_event_staging_store import DbStagingSink

_FIXTURES = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "discovery", "ingest", "fixtures",
)
_CT = os.path.join(_FIXTURES, "aws_cloudtrail_sample.json")


@pytest.fixture()
def conn():
    import sqlite3  # conftest routes this to PostgreSQL

    connection = sqlite3.connect("")
    try:
        yield connection
    finally:
        connection.close()


def _count(conn, org_id, batch_id):
    return conn.execute(
        "SELECT COUNT(*) AS n FROM ops_event_staging WHERE org_id = %s AND batch_id = %s",
        (org_id, batch_id),
    ).fetchall()[0]["n"]


def test_loader_writes_rows_and_reload_is_idempotent(conn):
    org = "org_ct_load"
    batch = "aws:cloudtrail:contract"
    sink = DbStagingSink()

    first = load_cloudtrail_logs(_CT, org_id=org, batch_id=batch, sink=sink)
    assert first.inserted_count == 3
    assert _count(conn, org, batch) == 3

    # Re-loading the same export batch inserts nothing new (AC3).
    second = load_cloudtrail_logs(_CT, org_id=org, batch_id=batch, sink=sink)
    assert second.inserted_count == 0
    assert second.duplicate_count == 3
    assert _count(conn, org, batch) == 3


def test_batch_registry_row_is_written(conn):
    org = "org_ct_batch"
    batch = "aws:cloudtrail:registry"
    load_cloudtrail_logs(_CT, org_id=org, batch_id=batch, sink=DbStagingSink())

    row = conn.execute(
        "SELECT record_count, skipped_count, provider, source_format "
        "FROM ops_event_load_batches WHERE org_id = %s AND batch_id = %s",
        (org, batch),
    ).fetchall()[0]
    assert row["record_count"] == 3
    assert row["skipped_count"] == 0
    assert row["provider"] == "aws"
    assert row["source_format"] == "cloudtrail"


def test_same_event_id_isolated_across_orgs(conn):
    records = [{"eventID": "shared-evt", "eventName": "X"}]
    sink = DbStagingSink()
    load_cloudtrail_logs(records, org_id="org_iso_A", batch_id="b", sink=sink)
    load_cloudtrail_logs(records, org_id="org_iso_B", batch_id="b", sink=sink)

    assert _count(conn, "org_iso_A", "b") == 1
    assert _count(conn, "org_iso_B", "b") == 1
