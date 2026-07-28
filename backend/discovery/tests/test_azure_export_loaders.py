"""Offline tests for the Azure export loaders — MSP-B8 / T3.

DB-free: drives the loaders against an :class:`InMemoryStagingSink` that enforces
the SAME ``(org_id, provider, provider_event_id)`` unique key as the database, so
idempotency/dedupe are proven without a database. Real-DB idempotency is in
``tests/contract/test_ops_event_staging_load.py``.

Covers the T3 acceptance criteria:
  * Azure Monitor + Activity Log samples load into staging,
  * provider / batch id / provider_event_id / event_time / raw / load metadata
    written per record,
  * raw payload preserved intact,
  * loud-skip discipline (reason + count, batch continues),
  * idempotent reload + within-batch dedupe,
  * org scoping.
"""
from __future__ import annotations

import os

import pytest

from discovery.ingest.azure_export_loaders import (
    MALFORMED_JSON,
    MISSING_EVENT_ID,
    NOT_AN_OBJECT,
    PROVIDER_AZURE,
    SOURCE_FORMAT_AZURE_ACTIVITY_LOG,
    SOURCE_FORMAT_AZURE_MONITOR,
    load_azure_activity_log,
    load_azure_monitor_alerts,
)
from discovery.ingest.ops_event_staging_store import InMemoryStagingSink

_FIXTURES = os.path.join(os.path.dirname(os.path.dirname(__file__)), "ingest", "fixtures")
_MON = os.path.join(_FIXTURES, "azure_monitor_alerts_sample.json")
_ACT = os.path.join(_FIXTURES, "azure_activity_log_sample.json")


# ---------------------------------------------------------------------------
# Happy path — real fixtures load
# ---------------------------------------------------------------------------


def test_azure_monitor_fixture_loads():
    sink = InMemoryStagingSink()
    res = load_azure_monitor_alerts(_MON, org_id="default", sink=sink)
    assert res.record_count == 2
    assert res.inserted_count == 2
    assert res.skipped_count == 0
    assert {r.provider for r in sink.rows} == {PROVIDER_AZURE}
    assert {r.source_format for r in sink.rows} == {SOURCE_FORMAT_AZURE_MONITOR}


def test_azure_activity_log_fixture_loads_and_uses_event_data_id():
    sink = InMemoryStagingSink()
    res = load_azure_activity_log(_ACT, org_id="default", sink=sink)
    assert res.inserted_count == 2
    ids = {r.provider_event_id for r in sink.rows}
    assert "9a8b7c6d-1111-4e2f-9a0b-1c2d3e4f5a6b" in ids
    assert res.source_format == SOURCE_FORMAT_AZURE_ACTIVITY_LOG


# ---------------------------------------------------------------------------
# Per-record staging metadata: provider_event_id + event_time + raw intact
# ---------------------------------------------------------------------------


def test_monitor_writes_event_time_from_fired_datetime():
    sink = InMemoryStagingSink()
    load_azure_monitor_alerts(_MON, org_id="default", sink=sink)
    row = sink.rows[0]
    assert row.event_time is not None
    assert row.event_time.year == 2026 and row.event_time.month == 6
    assert row.event_time.tzinfo is not None  # timezone-aware (UTC)


def test_activity_log_parses_seven_digit_fractional_timestamp():
    # Azure uses 100-ns (7 fractional digit) timestamps that datetime.fromisoformat
    # rejects; the loader truncates to microseconds rather than dropping the time.
    sink = InMemoryStagingSink()
    load_azure_activity_log(_ACT, org_id="default", sink=sink)
    row = next(r for r in sink.rows if r.provider_event_id == "9a8b7c6d-1111-4e2f-9a0b-1c2d3e4f5a6b")
    assert row.event_time is not None
    assert row.event_time.microsecond == 123456


def test_raw_payload_preserved_intact():
    sink = InMemoryStagingSink()
    load_azure_activity_log(_ACT, org_id="default", sink=sink)
    row = next(r for r in sink.rows if r.provider_event_id == "9a8b7c6d-1111-4e2f-9a0b-1c2d3e4f5a6b")
    assert row.raw["operationName"]["value"] == "Microsoft.Compute/virtualMachines/write"
    assert row.raw["status"]["value"] == "Succeeded"


def test_monitor_handles_alerts_api_resource_envelope():
    # The Alerts-Management-API shape: id + properties.essentials (not data.essentials).
    records = [
        {
            "id": "/subscriptions/s/providers/Microsoft.AlertsManagement/alerts/api-1",
            "name": "api-1",
            "properties": {
                "essentials": {
                    "alertId": "api-1",
                    "severity": "Sev1",
                    "startDateTime": "2026-06-02T09:00:00Z",
                }
            },
        }
    ]
    sink = InMemoryStagingSink()
    res = load_azure_monitor_alerts(records, org_id="default", sink=sink)
    assert res.inserted_count == 1
    row = sink.rows[0]
    # id preferred over the essentials.alertId; time from startDateTime.
    assert row.provider_event_id.endswith("alerts/api-1")
    assert row.event_time is not None


# ---------------------------------------------------------------------------
# Idempotency + dedupe
# ---------------------------------------------------------------------------


def test_reloading_same_batch_inserts_zero_duplicates():
    sink = InMemoryStagingSink()
    first = load_azure_monitor_alerts(_MON, org_id="default", sink=sink)
    assert first.inserted_count == 2
    before = len(sink.rows)
    second = load_azure_monitor_alerts(_MON, org_id="default", sink=sink)
    assert second.inserted_count == 0
    assert second.duplicate_count == 2
    assert len(sink.rows) == before


def test_within_batch_duplicate_collapses():
    records = [
        {"eventDataId": "d-1", "eventTimestamp": "2026-06-01T00:00:00Z"},
        {"eventDataId": "d-1", "eventTimestamp": "2026-06-01T00:00:00Z"},
        {"eventDataId": "d-2", "eventTimestamp": "2026-06-01T01:00:00Z"},
    ]
    sink = InMemoryStagingSink()
    res = load_azure_activity_log(records, org_id="default", sink=sink)
    assert res.inserted_count == 2
    assert res.duplicate_count == 1


# ---------------------------------------------------------------------------
# Loud-skip discipline
# ---------------------------------------------------------------------------


def test_missing_event_id_is_loud_skipped_and_batch_continues():
    records = [
        {"eventDataId": "ok-1", "eventTimestamp": "2026-06-01T00:00:00Z"},
        {"eventTimestamp": "2026-06-01T00:00:00Z"},  # no eventDataId / id
        {"eventDataId": "ok-2"},
    ]
    sink = InMemoryStagingSink()
    res = load_azure_activity_log(records, org_id="default", sink=sink)
    assert res.inserted_count == 2
    assert res.skipped_count == 1
    assert res.skipped[0].reason == MISSING_EVENT_ID


def test_non_object_record_is_loud_skipped():
    records = [{"eventDataId": "ok"}, "nope", 7]
    sink = InMemoryStagingSink()
    res = load_azure_activity_log(records, org_id="default", sink=sink)
    assert res.inserted_count == 1
    assert sorted(s.reason for s in res.skipped) == [NOT_AN_OBJECT, NOT_AN_OBJECT]


def test_malformed_json_line_is_loud_skipped_but_batch_continues(tmp_path):
    p = tmp_path / "activity.jsonl"
    p.write_text(
        '{"eventDataId": "line-1", "eventTimestamp": "2026-06-01T00:00:00Z"}\n'
        "{ not valid json }\n"
        '{"eventDataId": "line-3"}\n',
        encoding="utf-8",
    )
    sink = InMemoryStagingSink()
    res = load_azure_activity_log(str(p), org_id="default", sink=sink)
    assert res.inserted_count == 2
    assert res.skipped_count == 1
    assert res.skipped[0].reason == MALFORMED_JSON


def test_unparseable_event_time_is_left_null_not_a_skip():
    # event_time is best-effort ("where available"); a bad time never drops a row.
    records = [{"eventDataId": "t-1", "eventTimestamp": "not-a-timestamp"}]
    sink = InMemoryStagingSink()
    res = load_azure_activity_log(records, org_id="default", sink=sink)
    assert res.inserted_count == 1
    assert res.skipped_count == 0
    assert sink.rows[0].event_time is None


# ---------------------------------------------------------------------------
# Batch registry + org scoping
# ---------------------------------------------------------------------------


def test_batch_registry_records_counts():
    records = [{"eventDataId": "a"}, {"nope": 1}, {"eventDataId": "b"}]
    sink = InMemoryStagingSink()
    load_azure_activity_log(records, org_id="orgZ", batch_id="azure:activity:2026-06", sink=sink)
    batch = sink.batches[("orgZ", "azure:activity:2026-06")]
    assert batch.record_count == 2
    assert batch.skipped_count == 1
    assert batch.provider == PROVIDER_AZURE
    assert batch.source_format == SOURCE_FORMAT_AZURE_ACTIVITY_LOG


def test_default_batch_id_is_stable_and_azure_prefixed():
    sink = InMemoryStagingSink()
    a = load_azure_monitor_alerts(_MON, org_id="default", sink=sink)
    b = load_azure_monitor_alerts(_MON, org_id="default", sink=sink)
    assert a.batch_id == b.batch_id
    assert a.batch_id.startswith(f"{PROVIDER_AZURE}:{SOURCE_FORMAT_AZURE_MONITOR}:")


def test_same_event_id_isolated_across_orgs():
    records = [{"eventDataId": "shared", "eventTimestamp": "2026-06-01T00:00:00Z"}]
    sink = InMemoryStagingSink()
    a = load_azure_activity_log(records, org_id="org_A", sink=sink)
    b = load_azure_activity_log(records, org_id="org_B", sink=sink)
    assert a.inserted_count == 1 and b.inserted_count == 1
    assert len(sink.rows_for("org_A")) == 1
    assert len(sink.rows_for("org_B")) == 1


def test_org_id_is_required():
    with pytest.raises(ValueError):
        load_azure_monitor_alerts([{"id": "x"}], org_id="", sink=InMemoryStagingSink())


# ---------------------------------------------------------------------------
# Cross-provider equivalence: Azure and AWS produce the same staging shape
# ---------------------------------------------------------------------------


def test_azure_and_aws_rows_share_the_same_shape_differing_only_by_provider():
    from discovery.ingest.aws_export_loaders import load_cloudtrail_logs

    az = InMemoryStagingSink()
    aws = InMemoryStagingSink()
    load_azure_activity_log(
        [{"eventDataId": "z", "eventTimestamp": "2026-06-01T00:00:00Z"}],
        org_id="default", sink=az,
    )
    load_cloudtrail_logs(
        [{"eventID": "z", "eventTime": "2026-06-01T00:00:00Z"}],
        org_id="default", sink=aws,
    )
    az_row, aws_row = az.rows[0], aws.rows[0]
    assert vars(az_row).keys() == vars(aws_row).keys()      # identical field set
    assert az_row.provider == "azure" and aws_row.provider == "aws"
    assert az_row.event_time == aws_row.event_time          # same normalised time
