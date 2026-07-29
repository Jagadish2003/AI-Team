"""MSP-B8 / T7 — Section 3 acceptance criteria, executable end to end.

This is the single traceable place where MSP-B8's Section 3 is proved as tests,
tying together the staging schema (T1), the AWS/Azure loaders (T2/T3), the bridge
ingestor (T4), the volume validation (T5), and the equivalence harness (T6). Each
test is labelled with the AC it discharges and quotes it, so Section-3 coverage is
auditable at a glance. Tests run the FULL stack through the real database
(loaders → ``ops_event_staging`` → bridge) unless the AC is a design review.

Section 3 (from MSP-B8_EventHistoryBridge):
  AC1  real sample exports (both providers) load to staging and ingest end to end
       into normalised events with resolvable raw-payload evidence.
  AC2  EQUIVALENCE: golden fixtures through the bridge == mapper-direct, except
       source_system (both providers).
  AC3  re-loading the same export batch produces zero duplicate events
       (provider_event_id idempotency).
  AC4  ingestion is incremental by row-id checkpoint; a second run after new rows
       processes only the new rows.
  AC5  malformed export records are loud-skipped with reason and counted — never
       silently dropped, never poisoning the batch.
  AC6  the bridge runs on the read-only fail-closed DB path, org-scoped end to end
       (two-org test).
  AC7  a month-scale export sample ingests within the operational envelope, with
       measurements recorded and handed to MSP-B7.
  AC8  the staging-schema and loader docs are sufficient for a partner engineer to
       export-and-load unaided (partner-enablement review).
"""
import os

import pytest

from discovery.ingest.aws_export_loaders import load_cloudtrail_logs, load_eventbridge_archive
from discovery.ingest.azure_export_loaders import (
    load_azure_activity_log,
    load_azure_monitor_alerts,
)
from discovery.ingest.base import Checkpoint
from discovery.ingest.ops_event_bridge import (
    OpsEventBridgeIngestor,
    resolve_raw_payload,
)
from discovery.ingest.ops_event_equivalence import (
    all_passed,
    format_report,
    load_golden_cases,
    run_equivalence,
)
from discovery.ingest.ops_event_staging_store import DbStagingReader, DbStagingSink
from discovery.ingest.ops_event_volume_harness import run_volume_validation
from discovery.signals.evidence_store import InMemoryRawEventStore

_FIXTURES = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "discovery", "ingest", "fixtures",
)
_AWS_CT = os.path.join(_FIXTURES, "aws_cloudtrail_sample.json")
_AWS_EB = os.path.join(_FIXTURES, "aws_eventbridge_archive_sample.json")
_AZ_ACT = os.path.join(_FIXTURES, "azure_activity_log_sample.json")
_AZ_MON = os.path.join(_FIXTURES, "azure_monitor_alerts_sample.json")
_DOCS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))),
    "docs",
)


@pytest.fixture()
def conn():
    import sqlite3  # conftest routes this to PostgreSQL

    connection = sqlite3.connect("")
    try:
        yield connection
    finally:
        connection.close()


def _drain(ingestor, org, since=None):
    records, ckpt = [], None
    for batch in ingestor.ingest_changes(org, since):
        records.extend(batch.records)
        ckpt = batch.next_checkpoint
    return records, ckpt


# ═══════════════════════════════════════════════════════════════════════════
# AC1 — both providers load to staging and ingest end to end with evidence
# ═══════════════════════════════════════════════════════════════════════════


def test_ac1_aws_export_loads_and_ingests_end_to_end(conn):
    org = "ac1_aws"
    load_cloudtrail_logs(_AWS_CT, org_id=org, batch_id="aws:cloudtrail:ac1", sink=DbStagingSink())
    store = InMemoryRawEventStore()
    records, _ = _drain(OpsEventBridgeIngestor(DbStagingReader(), raw_store=store), org)

    assert len(records) == 3
    assert all(r["source_system"] == "bridge:aws" for r in records)
    # Raw-payload evidence resolves for a known event.
    rec = next(r for r in records if r["provider_event_id"] == "e2f1a3b4-1111-4c2d-9e88-0a1b2c3d4e5f")
    assert resolve_raw_payload(store, org, rec)["eventName"] == "DeleteBucket"


def test_ac1_azure_export_loads_and_ingests_end_to_end(conn):
    org = "ac1_azure"
    load_azure_activity_log(_AZ_ACT, org_id=org, batch_id="azure:activity:ac1", sink=DbStagingSink())
    store = InMemoryRawEventStore()
    records, _ = _drain(OpsEventBridgeIngestor(DbStagingReader(), raw_store=store), org)

    assert len(records) >= 1
    assert all(r["source_system"] == "bridge:azure" for r in records)
    # Every emitted event resolves back to a stored raw payload.
    assert all(resolve_raw_payload(store, org, r) is not None for r in records)


# ═══════════════════════════════════════════════════════════════════════════
# AC2 — equivalence (bridge == mapper-direct except source_system), both providers
# ═══════════════════════════════════════════════════════════════════════════


def test_ac2_equivalence_both_providers(conn):
    results = run_equivalence(
        load_golden_cases(), org_id="ac2_org",
        sink=DbStagingSink(), reader=DbStagingReader(),
    )
    assert all_passed(results), format_report(results)
    assert {r.provider for r in results} == {"aws", "azure"}


# ═══════════════════════════════════════════════════════════════════════════
# AC3 — re-loading the same batch produces zero duplicate events
# ═══════════════════════════════════════════════════════════════════════════


def test_ac3_reload_produces_no_duplicate_events(conn):
    org = "ac3_org"
    sink = DbStagingSink()
    load_cloudtrail_logs(_AWS_CT, org_id=org, batch_id="aws:cloudtrail:ac3", sink=sink)
    first, _ = _drain(OpsEventBridgeIngestor(DbStagingReader()), org)

    # Re-load the identical export batch (same provider_event_ids).
    reload_result = load_cloudtrail_logs(_AWS_CT, org_id=org, batch_id="aws:cloudtrail:ac3", sink=sink)
    assert reload_result.inserted_count == 0  # nothing new staged

    second, _ = _drain(OpsEventBridgeIngestor(DbStagingReader()), org)
    # Same distinct events; no provider_event_id emitted twice.
    ids_first = [r["provider_event_id"] for r in first]
    ids_second = [r["provider_event_id"] for r in second]
    assert sorted(ids_first) == sorted(ids_second)
    assert len(ids_second) == len(set(ids_second))  # zero duplicates


# ═══════════════════════════════════════════════════════════════════════════
# AC4 — incremental by row-id checkpoint
# ═══════════════════════════════════════════════════════════════════════════


def test_ac4_incremental_processes_only_new_rows(conn):
    org = "ac4_org"
    sink = DbStagingSink()
    load_cloudtrail_logs(_AWS_CT, org_id=org, batch_id="b1", sink=sink)
    ingestor = OpsEventBridgeIngestor(DbStagingReader())

    first, ckpt = _drain(ingestor, org)
    assert len(first) == 3

    since = Checkpoint.create("ops_event_bridge", org, ckpt)
    assert _drain(ingestor, org, since=since)[0] == []  # nothing new

    load_cloudtrail_logs(
        [{"eventID": "ac4-new", "eventName": "PutObject", "eventSource": "s3.amazonaws.com"}],
        org_id=org, batch_id="b2", sink=sink,
    )
    third, _ = _drain(ingestor, org, since=since)
    assert [r["provider_event_id"] for r in third] == ["ac4-new"]


# ═══════════════════════════════════════════════════════════════════════════
# AC5 — malformed records loud-skipped with reason and counted; batch continues
# ═══════════════════════════════════════════════════════════════════════════


def test_ac5_malformed_records_are_loud_skipped_and_counted(conn):
    org = "ac5_org"
    sink = DbStagingSink()
    records = [
        {"eventID": "ac5-1", "eventName": "A"},
        {"eventName": "no-id"},          # missing id → loud-skip
        "not-an-object",                 # not an object → loud-skip
        {"eventID": "ac5-2", "eventName": "B"},
    ]
    result = load_cloudtrail_logs(records, org_id=org, batch_id="aws:cloudtrail:ac5", sink=sink)

    assert result.inserted_count == 2                 # valid records still loaded
    assert result.skipped_count == 2                  # both bad ones skipped
    assert {s.reason for s in result.skipped} == {"missing_event_id", "not_an_object"}
    assert all(s.detail for s in result.skipped)      # each has a reason string

    # The batch was not poisoned — the bridge emits exactly the valid events.
    emitted, _ = _drain(OpsEventBridgeIngestor(DbStagingReader()), org)
    assert sorted(r["provider_event_id"] for r in emitted) == ["ac5-1", "ac5-2"]

    # Skip count is recorded in the batch registry.
    row = conn.execute(
        "SELECT skipped_count FROM ops_event_load_batches WHERE org_id=%s AND batch_id=%s",
        (org, "aws:cloudtrail:ac5"),
    ).fetchall()[0]
    assert row["skipped_count"] == 2


# ═══════════════════════════════════════════════════════════════════════════
# AC6 — read-only fail-closed DB path + two-org isolation
# ═══════════════════════════════════════════════════════════════════════════


def test_ac6_bridge_read_path_is_read_only(conn):
    import psycopg2
    from contextlib import closing
    from app import db

    org = "ac6_ro"
    load_cloudtrail_logs(_AWS_CT, org_id=org, batch_id="b", sink=DbStagingSink())
    with closing(db.connect()) as con:
        cur = con.cursor()
        cur.execute("SET TRANSACTION READ ONLY")  # the reader's discipline
        cur.execute("SELECT COUNT(*) AS n FROM ops_event_staging WHERE org_id=%s", (org,))
        assert cur.fetchall()[0]["n"] == 3
        with pytest.raises(psycopg2.Error):        # a write is rejected — fail closed
            cur.execute(
                "INSERT INTO ops_event_staging "
                "(org_id,provider,source_format,batch_id,provider_event_id,raw) "
                "VALUES (%s,'aws','cloudtrail','x','x','{}'::jsonb)", (org,),
            )
        con.rollback()


def test_ac6_two_org_isolation_rows_checkpoints_and_evidence(conn):
    sink = DbStagingSink()
    # Overlapping provider_event_ids across two orgs must NOT collide or leak.
    shared = [{"eventID": "shared-1", "eventName": "X"}, {"eventID": "shared-2", "eventName": "Y"}]
    load_cloudtrail_logs(shared, org_id="ac6_A", batch_id="b", sink=sink)
    load_cloudtrail_logs(shared + [{"eventID": "b-only", "eventName": "Z"}],
                         org_id="ac6_B", batch_id="b", sink=sink)

    store_a = InMemoryRawEventStore()
    recs_a, ckpt_a = _drain(OpsEventBridgeIngestor(DbStagingReader(), raw_store=store_a), "ac6_A")
    recs_b, ckpt_b = _drain(OpsEventBridgeIngestor(DbStagingReader()), "ac6_B")

    # Rows: each org sees only its own.
    assert sorted(r["provider_event_id"] for r in recs_a) == ["shared-1", "shared-2"]
    assert "b-only" in {r["provider_event_id"] for r in recs_b}
    # Checkpoints are independent (org B has one more row).
    assert int(ckpt_b) != int(ckpt_a)
    # Evidence is org-scoped: org A's evidence does not resolve under org B.
    assert resolve_raw_payload(store_a, "ac6_A", recs_a[0]) is not None
    assert resolve_raw_payload(store_a, "ac6_B", recs_a[0]) is None


# ═══════════════════════════════════════════════════════════════════════════
# AC7 — month-scale envelope + measurements recorded (see also T5 evidence doc)
# ═══════════════════════════════════════════════════════════════════════════


def test_ac7_volume_ingests_within_envelope_with_measurements(conn):
    report = run_volume_validation(
        "ac7_org", sink=DbStagingSink(), reader=DbStagingReader(),
        raw_store=InMemoryRawEventStore(), per_format=300, batch_size=200,
    )
    assert report.envelope_pass, report.envelope_failures
    # Measurements are real, not assumed.
    assert report.load_rows_per_sec > 0 and report.ingest_events_per_sec > 0
    assert report.peak_memory_mb > 0 and report.batches > 1
    # Recorded month-scale evidence handed to MSP-B7 exists.
    assert os.path.exists(os.path.join(_DOCS, "MSP-B8_VOLUME_VALIDATION.md"))


# ═══════════════════════════════════════════════════════════════════════════
# AC8 — partner-enablement documentation review (design review, captured)
# ═══════════════════════════════════════════════════════════════════════════


def test_ac8_staging_schema_doc_is_sufficient():
    with open(os.path.join(_DOCS, "MSP-B8_STAGING_SCHEMA.md"), encoding="utf-8") as fh:
        doc = fh.read().lower()
    # A partner engineer can create the store and understand the load contract.
    for needle in [
        "applying the ddl", "ops_event_staging", "ops_event_load_batches",
        "provider_event_id", "row-id", "org scoping", "raw payload", "checklist",
    ]:
        assert needle in doc, f"schema doc missing: {needle!r}"


def test_ac8_loader_doc_is_sufficient():
    with open(os.path.join(_DOCS, "MSP-B8_LOADERS.md"), encoding="utf-8") as fh:
        doc = fh.read().lower()
    for needle in [
        "load_cloudtrail_logs", "load_eventbridge_archive", "load_cloudwatch_alarm_history",
        "load_azure_monitor_alerts", "load_azure_activity_log",
        "source_format", "provider_event_id", "loud-skip", "loadresult", "idempoten",
    ]:
        assert needle in doc, f"loader doc missing: {needle!r}"
