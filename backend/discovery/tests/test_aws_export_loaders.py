"""Offline tests for the AWS export loaders — MSP-B8 / T2.

DB-free: every test drives the loaders against an :class:`InMemoryStagingSink`
that enforces the SAME ``(org_id, provider, provider_event_id)`` unique key as the
database, so idempotency and dedupe are proven without a database (the offline
discovery test rule). Real-DB idempotency is exercised separately in
``tests/contract/test_ops_event_staging_load.py``.

Covers the T2 acceptance criteria:
  * real AWS export samples load into staging (fixtures),
  * raw payload preserved intact + provider metadata for mapping/dedupe,
  * loud-skip discipline: bad records skipped with reason, counted, valid ones
    still load,
  * idempotent at batch and provider-event level (re-load = zero new rows),
  * within-batch duplicate collapse,
  * batch registry records record/skip counts,
  * org scoping.
"""
from __future__ import annotations

import gzip
import json
import os

import pytest

from discovery.ingest.aws_export_loaders import (
    MALFORMED_JSON,
    MISSING_EVENT_ID,
    NOT_AN_OBJECT,
    PROVIDER_AWS,
    SOURCE_FORMAT_CLOUDTRAIL,
    SOURCE_FORMAT_CLOUDWATCH,
    SOURCE_FORMAT_EVENTBRIDGE,
    load_cloudtrail_logs,
    load_cloudwatch_alarm_history,
    load_eventbridge_archive,
)
from discovery.ingest.ops_event_staging_store import InMemoryStagingSink

_FIXTURES = os.path.join(os.path.dirname(os.path.dirname(__file__)), "ingest", "fixtures")
_CW = os.path.join(_FIXTURES, "aws_cloudwatch_alarm_history_sample.json")
_EB = os.path.join(_FIXTURES, "aws_eventbridge_archive_sample.json")
_CT = os.path.join(_FIXTURES, "aws_cloudtrail_sample.json")


# ---------------------------------------------------------------------------
# Happy path — real fixtures load into staging
# ---------------------------------------------------------------------------


def test_cloudwatch_fixture_loads():
    sink = InMemoryStagingSink()
    res = load_cloudwatch_alarm_history(_CW, org_id="default", sink=sink)
    assert res.record_count == 3
    assert res.inserted_count == 3
    assert res.skipped_count == 0
    assert {r.source_format for r in sink.rows} == {SOURCE_FORMAT_CLOUDWATCH}
    assert {r.provider for r in sink.rows} == {PROVIDER_AWS}


def test_eventbridge_fixture_loads_and_uses_envelope_id():
    sink = InMemoryStagingSink()
    res = load_eventbridge_archive(_EB, org_id="default", sink=sink)
    assert res.inserted_count == 3
    ids = {r.provider_event_id for r in sink.rows}
    assert "6a7e8feb-b491-4cf7-a9f1-bf3703467718" in ids


def test_cloudtrail_fixture_loads_and_uses_event_id():
    sink = InMemoryStagingSink()
    res = load_cloudtrail_logs(_CT, org_id="default", sink=sink)
    assert res.inserted_count == 3
    ids = {r.provider_event_id for r in sink.rows}
    assert "e2f1a3b4-1111-4c2d-9e88-0a1b2c3d4e5f" in ids
    assert res.source_format == SOURCE_FORMAT_CLOUDTRAIL


# ---------------------------------------------------------------------------
# Raw payload preserved intact
# ---------------------------------------------------------------------------


def test_raw_payload_is_preserved_intact():
    sink = InMemoryStagingSink()
    load_cloudtrail_logs(_CT, org_id="default", sink=sink)
    row = next(r for r in sink.rows if r.provider_event_id == "e2f1a3b4-1111-4c2d-9e88-0a1b2c3d4e5f")
    # The whole provider record survives verbatim for downstream mapping/evidence.
    assert row.raw["eventName"] == "DeleteBucket"
    assert row.raw["requestParameters"]["bucketName"] == "prod-logs-archive"
    assert row.raw["userIdentity"]["userName"] == "ops-admin"


# ---------------------------------------------------------------------------
# provider_event_id extraction — CloudWatch composite is deterministic
# ---------------------------------------------------------------------------


def test_cloudwatch_composite_id_is_deterministic_and_distinct():
    sink = InMemoryStagingSink()
    load_cloudwatch_alarm_history(_CW, org_id="default", sink=sink)
    ids = [r.provider_event_id for r in sink.rows]
    assert len(ids) == len(set(ids)) == 3  # distinct alarm/timestamp/type triples

    # Same input -> same id (stable identity => idempotent).
    sink2 = InMemoryStagingSink()
    load_cloudwatch_alarm_history(_CW, org_id="default", sink=sink2)
    assert sorted(r.provider_event_id for r in sink2.rows) == sorted(ids)


# ---------------------------------------------------------------------------
# Idempotency — batch level and provider-event level
# ---------------------------------------------------------------------------


def test_reloading_same_batch_inserts_zero_duplicates():
    sink = InMemoryStagingSink()
    first = load_cloudtrail_logs(_CT, org_id="default", sink=sink)
    assert first.inserted_count == 3
    before = len(sink.rows)

    second = load_cloudtrail_logs(_CT, org_id="default", sink=sink)
    assert second.record_count == 3          # still parsed
    assert second.inserted_count == 0        # nothing new
    assert second.duplicate_count == 3       # all already present
    assert len(sink.rows) == before          # staging unchanged


def test_within_batch_duplicate_collapses_to_one_row():
    records = [
        {"eventID": "dup-1", "eventName": "A"},
        {"eventID": "dup-1", "eventName": "A"},  # exact duplicate in one export
        {"eventID": "uniq-2", "eventName": "B"},
    ]
    sink = InMemoryStagingSink()
    res = load_cloudtrail_logs(records, org_id="default", sink=sink)
    assert res.inserted_count == 2
    assert res.duplicate_count == 1
    assert {r.provider_event_id for r in sink.rows} == {"dup-1", "uniq-2"}


# ---------------------------------------------------------------------------
# Loud-skip discipline (AC5)
# ---------------------------------------------------------------------------


def test_missing_event_id_is_loud_skipped_and_counted():
    records = [
        {"eventID": "ok-1", "eventName": "Good"},
        {"eventName": "NoId"},                    # missing eventID -> skip
        {"eventID": "ok-2", "eventName": "AlsoGood"},
    ]
    sink = InMemoryStagingSink()
    res = load_cloudtrail_logs(records, org_id="default", sink=sink)
    assert res.inserted_count == 2               # valid records still load
    assert res.skipped_count == 1
    assert res.skipped[0].reason == MISSING_EVENT_ID
    assert res.skipped[0].detail                 # a human reason is present


def test_non_object_record_is_loud_skipped():
    records = [{"eventID": "ok-1"}, "not-an-object", 42]
    sink = InMemoryStagingSink()
    res = load_cloudtrail_logs(records, org_id="default", sink=sink)
    assert res.inserted_count == 1
    assert sorted(s.reason for s in res.skipped) == [NOT_AN_OBJECT, NOT_AN_OBJECT]


def test_malformed_json_line_is_loud_skipped_but_batch_continues(tmp_path):
    # A JSON-lines file where one line is corrupt: the good lines still load.
    p = tmp_path / "trail.jsonl"
    p.write_text(
        '{"eventID": "line-1", "eventName": "Good"}\n'
        "{ this is not valid json }\n"
        '{"eventID": "line-3", "eventName": "AlsoGood"}\n',
        encoding="utf-8",
    )
    sink = InMemoryStagingSink()
    res = load_cloudtrail_logs(str(p), org_id="default", sink=sink)
    assert res.inserted_count == 2
    assert res.skipped_count == 1
    assert res.skipped[0].reason == MALFORMED_JSON


def test_bad_records_never_poison_the_batch_counts_add_up():
    records = [
        {"eventID": "a"},
        {"nope": True},           # missing id
        {"eventID": "b"},
        "junk",                   # not an object
        {"eventID": "a"},         # within-batch dup
    ]
    sink = InMemoryStagingSink()
    res = load_cloudtrail_logs(records, org_id="default", sink=sink)
    assert res.inserted_count == 2         # a, b
    assert res.duplicate_count == 1        # second "a"
    assert res.skipped_count == 2          # missing id + junk


# ---------------------------------------------------------------------------
# Batch registry (AC5 counting surface)
# ---------------------------------------------------------------------------


def test_batch_registry_records_counts():
    records = [{"eventID": "a"}, {"nope": 1}, {"eventID": "b"}]
    sink = InMemoryStagingSink()
    res = load_cloudtrail_logs(records, org_id="orgX", batch_id="aws:cloudtrail:2026-06", sink=sink)
    batch = sink.batches[("orgX", "aws:cloudtrail:2026-06")]
    assert batch.record_count == 2         # valid parsed
    assert batch.skipped_count == 1
    assert batch.provider == PROVIDER_AWS
    assert batch.source_format == SOURCE_FORMAT_CLOUDTRAIL
    assert res.batch_id == "aws:cloudtrail:2026-06"


def test_default_batch_id_is_stable_across_reloads():
    sink = InMemoryStagingSink()
    a = load_eventbridge_archive(_EB, org_id="default", sink=sink)
    b = load_eventbridge_archive(_EB, org_id="default", sink=sink)
    assert a.batch_id == b.batch_id
    assert a.batch_id.startswith(f"{PROVIDER_AWS}:{SOURCE_FORMAT_EVENTBRIDGE}:")


# ---------------------------------------------------------------------------
# Org scoping
# ---------------------------------------------------------------------------


def test_same_event_id_in_two_orgs_is_not_a_duplicate():
    records = [{"eventID": "shared-1", "eventName": "X"}]
    sink = InMemoryStagingSink()
    a = load_cloudtrail_logs(records, org_id="org_A", sink=sink)
    b = load_cloudtrail_logs(records, org_id="org_B", sink=sink)
    assert a.inserted_count == 1
    assert b.inserted_count == 1             # different org => not a dup
    assert len(sink.rows_for("org_A")) == 1
    assert len(sink.rows_for("org_B")) == 1


def test_org_id_is_required():
    with pytest.raises(ValueError):
        load_cloudtrail_logs([{"eventID": "a"}], org_id="", sink=InMemoryStagingSink())


# ---------------------------------------------------------------------------
# Directory loading + gzip (CloudTrail)
# ---------------------------------------------------------------------------


def test_cloudtrail_loads_directory_of_gzipped_files(tmp_path):
    f1 = tmp_path / "0001.json.gz"
    f2 = tmp_path / "0002.json.gz"
    with gzip.open(f1, "wt", encoding="utf-8") as fh:
        json.dump({"Records": [{"eventID": "g-1", "eventName": "A"}]}, fh)
    with gzip.open(f2, "wt", encoding="utf-8") as fh:
        json.dump({"Records": [{"eventID": "g-2", "eventName": "B"}]}, fh)

    sink = InMemoryStagingSink()
    res = load_cloudtrail_logs(str(tmp_path), org_id="default", sink=sink)
    assert res.inserted_count == 2
    assert {r.provider_event_id for r in sink.rows} == {"g-1", "g-2"}


def test_one_bad_file_in_directory_does_not_poison_the_others(tmp_path):
    good = tmp_path / "good.json"
    bad = tmp_path / "bad.json"
    good.write_text(json.dumps({"Records": [{"eventID": "ok"}]}), encoding="utf-8")
    bad.write_text("{ not json", encoding="utf-8")

    sink = InMemoryStagingSink()
    res = load_cloudtrail_logs(str(tmp_path), org_id="default", sink=sink)
    assert res.inserted_count == 1
    assert res.skipped_count == 1
    assert res.skipped[0].reason == MALFORMED_JSON
