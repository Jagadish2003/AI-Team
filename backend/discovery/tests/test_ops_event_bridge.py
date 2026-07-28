"""Offline tests for the Event-History Bridge ingestor — MSP-B8 / T4.

DB-free: staging is seeded through :class:`InMemoryStagingSink` (which mints
row_ids and serves as a :class:`StagingReader`), and evidence resolution uses the
in-memory :class:`InMemoryRawEventStore`. Real-DB / read-only-path behaviour is
covered in ``tests/contract/test_ops_event_bridge_contract.py``.

Covers the T4 acceptance criteria and "done" bar:
  * routes each staged (provider, source_format) through the right MSP-B0 mapper,
  * emits ``source_system='bridge:<provider>'`` and is otherwise IDENTICAL to the
    mapper-direct event (same event_signature — the equivalence guarantee),
  * preserves ``provider_event_id`` and dedupes on it,
  * attaches evidence resolving to the raw staged payload + batch identity,
  * ChangeBasedIngestor contract: row-id checkpoint paging, resume, empty delta,
  * fail-closed on a reader error (checkpoint not advanced),
  * org scoping.
"""
from __future__ import annotations

from typing import List

import pytest

from database.models.ops_event_staging import OpsEventStagingRow
from discovery.ingest.base import Checkpoint
from discovery.ingest.ops_event_bridge import (
    OpsEventBridgeIngestor,
    bridge_source_system,
    resolve_raw_payload,
)
from discovery.ingest.ops_event_staging_store import InMemoryStagingSink
from discovery.signals.evidence_store import InMemoryRawEventStore
from discovery.signals.reference_mappers import (
    map_azure_activity_log,
    map_azure_monitor,
    map_cloudtrail,
    map_cloudwatch,
    map_eventbridge,
)

ORG = "default"


# ---------------------------------------------------------------------------
# Representative provider payloads (each shaped for its B0 mapper)
# ---------------------------------------------------------------------------

_CLOUDWATCH = {
    "id": "cw-evt-1",
    "detail-type": "CloudWatch Alarm State Change",
    "time": "2026-06-01T12:00:00Z",
    "region": "us-east-1",
    "account": "111122223333",
    "resources": ["arn:aws:cloudwatch:us-east-1:111122223333:alarm:prod-api-5xx"],
    "detail": {
        "alarmName": "prod-api-5xx",
        "state": {"value": "ALARM", "reason": "Threshold Crossed"},
        "previousState": {"value": "OK"},
    },
}
_EVENTBRIDGE = {
    "version": "0",
    "id": "eb-evt-1",
    "detail-type": "EC2 Instance State-change Notification",
    "source": "aws.ec2",
    "account": "111122223333",
    "time": "2026-06-01T13:00:00Z",
    "region": "us-east-1",
    "resources": ["arn:aws:ec2:us-east-1:111122223333:instance/i-0abcd1234"],
    "detail": {"instance-id": "i-0abcd1234", "state": "stopped"},
}
_CLOUDTRAIL = {
    "eventID": "ct-evt-1",
    "eventName": "DeleteBucket",
    "eventTime": "2026-06-01T14:15:16Z",
    "eventSource": "s3.amazonaws.com",
    "awsRegion": "us-east-1",
    "userIdentity": {"type": "IAMUser", "arn": "arn:aws:iam::111122223333:user/ops"},
}
_AZURE_MONITOR = {
    "data": {
        "essentials": {
            "alertId": "/subscriptions/s/providers/Microsoft.AlertsManagement/alerts/az-mon-1",
            "alertRule": "HighCPU",
            "severity": "Sev2",
            "firedDateTime": "2026-06-01T15:00:00Z",
            "monitorCondition": "Fired",
            "alertTargetIDs": [
                "/subscriptions/s/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm1"
            ],
            "description": "CPU consistently above 90%",
        }
    }
}
_AZURE_ACTIVITY = {
    "eventDataId": "az-act-1",
    "operationName": "Microsoft.Compute/virtualMachines/write",
    "resourceId": "/subscriptions/s/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm1",
    "caller": "admin@contoso.com",
    "level": "Informational",
    "status": {"value": "Succeeded"},
    "eventTimestamp": "2026-06-01T16:00:00Z",
    "category": {"value": "Administrative"},
}

# (provider, source_format, provider_event_id, raw_payload, mapper-direct fn)
_CASES = [
    ("aws", "cloudwatch_alarm_history", "cw-evt-1", _CLOUDWATCH, map_cloudwatch),
    ("aws", "eventbridge_archive", "eb-evt-1", _EVENTBRIDGE, map_eventbridge),
    ("aws", "cloudtrail", "ct-evt-1", _CLOUDTRAIL, map_cloudtrail),
    ("azure", "azure_monitor", "az-mon-1", _AZURE_MONITOR, map_azure_monitor),
    ("azure", "azure_activity_log", "az-act-1", _AZURE_ACTIVITY, map_azure_activity_log),
]


def _row(provider, source_format, peid, raw, org=ORG):
    return OpsEventStagingRow(
        org_id=org,
        provider=provider,
        source_format=source_format,
        batch_id=f"{provider}:{source_format}:test-batch",
        provider_event_id=peid,
        raw=raw,
    )


def _seed(rows) -> InMemoryStagingSink:
    sink = InMemoryStagingSink()
    sink.insert_rows(rows)
    return sink


def _drain(ingestor, org=ORG, since=None):
    records = []
    last_ckpt = None
    for batch in ingestor.ingest_changes(org, since):
        records.extend(batch.records)
        last_ckpt = batch.next_checkpoint
    return records, last_ckpt


class _ListReader:
    """Minimal StagingReader over a fixed list — for dedupe / fail-closed tests."""

    def __init__(self, rows, *, raise_on_call=False):
        self._rows = rows
        self._raise = raise_on_call

    def fetch_after(self, org_id, *, after_row_id, limit):
        if self._raise:
            raise RuntimeError("simulated read-only DB failure")
        out = [r for r in self._rows if (r.row_id or 0) > after_row_id and r.org_id == org_id]
        return sorted(out, key=lambda r: r.row_id or 0)[:limit]


# ---------------------------------------------------------------------------
# Routing + source_system prefix + equivalence to mapper-direct
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("provider,source_format,peid,raw,mapper", _CASES)
def test_bridge_routes_and_prefixes_and_matches_native(provider, source_format, peid, raw, mapper):
    sink = _seed([_row(provider, source_format, peid, raw)])
    ingestor = OpsEventBridgeIngestor(sink)
    records, _ = _drain(ingestor)

    assert len(records) == 1
    rec = records[0]
    expected_ss = bridge_source_system(provider)
    assert rec["source_system"] == expected_ss
    assert rec["event"]["source_system"] == expected_ss
    assert rec["provider_event_id"] == peid

    # Detector-visible equivalence: identical to the mapper-direct event EXCEPT
    # source_system (and its transport-specific provenance). This is the bridge
    # equivalence guarantee (AC2, finalised field-by-field in T6).
    native = mapper(raw, org_id=ORG).to_dict()
    bridged = rec["event"]
    differing = {k for k in native if native[k] != bridged.get(k)}
    assert differing == {"source_system", "provenance"}


@pytest.mark.parametrize("provider,source_format,peid,raw,mapper", _CASES)
def test_event_signature_preserved_from_native(provider, source_format, peid, raw, mapper):
    sink = _seed([_row(provider, source_format, peid, raw)])
    records, _ = _drain(OpsEventBridgeIngestor(sink))
    native = mapper(raw, org_id=ORG)
    # The recurrence signature is derived from the native provider family; the
    # bridge must NOT recompute it under 'bridge:...' (which would fall to the
    # 'generic' family and diverge).
    assert records[0]["event"]["event_signature"] == native.event_signature
    assert records[0]["event"]["event_signature"]  # non-empty


# ---------------------------------------------------------------------------
# Evidence: resolves to raw staged payload + batch identity
# ---------------------------------------------------------------------------


def test_evidence_resolves_to_raw_staged_payload_and_batch():
    sink = _seed([_row("aws", "cloudtrail", "ct-evt-1", _CLOUDTRAIL)])
    store = InMemoryRawEventStore()
    records, _ = _drain(OpsEventBridgeIngestor(sink, raw_store=store))
    rec = records[0]

    # Batch identity travels on the record.
    assert rec["batch_id"] == "aws:cloudtrail:test-batch"
    assert rec["staging_row_id"] == 1

    # Evidence pointer resolves back to the exact raw staged payload.
    resolved = resolve_raw_payload(store, ORG, rec)
    assert resolved == _CLOUDTRAIL
    assert rec["evidence_pointer"]["source_system"] == "bridge:aws"
    assert rec["evidence_pointer"]["origin"] == "observed"


def test_evidence_is_org_scoped():
    sink = _seed([_row("aws", "cloudtrail", "ct-evt-1", _CLOUDTRAIL)])
    store = InMemoryRawEventStore()
    records, _ = _drain(OpsEventBridgeIngestor(sink, raw_store=store))
    # Wrong org cannot resolve another org's raw payload.
    assert resolve_raw_payload(store, "other_org", records[0]) is None


# ---------------------------------------------------------------------------
# Checkpoint paging / resume (ChangeBasedIngestor contract)
# ---------------------------------------------------------------------------


def test_pages_by_row_id_and_flags_final_batch():
    rows = [_row("aws", "cloudtrail", f"ct-{i}", {"eventID": f"ct-{i}"}) for i in range(5)]
    sink = _seed(rows)
    ingestor = OpsEventBridgeIngestor(sink, batch_size=2)

    batches = list(ingestor.ingest_changes(ORG, None))
    assert [len(b.records) for b in batches] == [2, 2, 1]
    assert [b.is_complete for b in batches] == [False, False, True]
    # Checkpoint advances monotonically to the last row_id.
    assert [b.next_checkpoint for b in batches] == ["2", "4", "5"]


def test_resume_from_checkpoint_reads_only_new_rows():
    rows = [_row("aws", "cloudtrail", f"ct-{i}", {"eventID": f"ct-{i}"}) for i in range(3)]
    sink = _seed(rows)
    ingestor = OpsEventBridgeIngestor(sink, batch_size=10)

    records, ckpt = _drain(ingestor)
    assert len(records) == 3 and ckpt == "3"

    # Re-running from the checkpoint yields an empty delta echoing the position.
    since = Checkpoint.create("ops_event_bridge", ORG, ckpt)
    records2, ckpt2 = _drain(ingestor, since=since)
    assert records2 == [] and ckpt2 == "3"

    # New rows arrive → only those are processed on resume.
    sink.insert_rows([_row("aws", "cloudtrail", "ct-9", {"eventID": "ct-9"})])
    records3, ckpt3 = _drain(ingestor, since=since)
    assert [r["provider_event_id"] for r in records3] == ["ct-9"]
    assert ckpt3 == "4"


def test_empty_staging_yields_single_empty_delta():
    ingestor = OpsEventBridgeIngestor(InMemoryStagingSink())
    batches = list(ingestor.ingest_changes(ORG, None))
    assert len(batches) == 1
    assert batches[0].is_empty and batches[0].is_complete
    assert batches[0].next_checkpoint == "0"


# ---------------------------------------------------------------------------
# Dedupe by provider_event_id (idempotent loads)
# ---------------------------------------------------------------------------


def test_duplicate_provider_event_id_collapses_to_one_event():
    # Two staged rows sharing (provider, provider_event_id) — fed via a stub
    # reader since the store's unique key would normally prevent this.
    r1 = _row("aws", "cloudtrail", "dup-1", {"eventID": "dup-1", "eventName": "A"})
    r1.row_id = 1
    r2 = _row("aws", "cloudtrail", "dup-1", {"eventID": "dup-1", "eventName": "A"})
    r2.row_id = 2
    ingestor = OpsEventBridgeIngestor(_ListReader([r1, r2]), batch_size=10)
    records, _ = _drain(ingestor)
    assert len(records) == 1
    assert records[0]["provider_event_id"] == "dup-1"


# ---------------------------------------------------------------------------
# Loud-skip unroutable rows; fail-closed on reader error
# ---------------------------------------------------------------------------


def test_unroutable_row_is_skipped_but_checkpoint_advances():
    good = _row("aws", "cloudtrail", "ct-1", _CLOUDTRAIL)
    bad = _row("gcp", "stackdriver", "gcp-1", {"x": 1})  # no mapper registered
    sink = _seed([good, bad])
    records, ckpt = _drain(OpsEventBridgeIngestor(sink, batch_size=10))
    assert [r["provider_event_id"] for r in records] == ["ct-1"]
    assert ckpt == "2"  # advanced past the skipped row, not wedged


def test_reader_error_is_fail_closed():
    rows = [_row("aws", "cloudtrail", "ct-1", _CLOUDTRAIL)]
    rows[0].row_id = 1
    ingestor = OpsEventBridgeIngestor(_ListReader(rows, raise_on_call=True))
    with pytest.raises(RuntimeError):
        list(ingestor.ingest_changes(ORG, None))


# ---------------------------------------------------------------------------
# Org scoping + guards
# ---------------------------------------------------------------------------


def test_org_scoped_reads():
    sink = InMemoryStagingSink()
    sink.insert_rows([_row("aws", "cloudtrail", "a-1", {"eventID": "a-1"}, org="org_A")])
    sink.insert_rows([_row("aws", "cloudtrail", "b-1", {"eventID": "b-1"}, org="org_B")])

    recs_a, _ = _drain(OpsEventBridgeIngestor(sink), org="org_A")
    recs_b, _ = _drain(OpsEventBridgeIngestor(sink), org="org_B")
    assert [r["provider_event_id"] for r in recs_a] == ["a-1"]
    assert [r["provider_event_id"] for r in recs_b] == ["b-1"]


def test_org_id_required():
    with pytest.raises(ValueError):
        list(OpsEventBridgeIngestor(InMemoryStagingSink()).ingest_changes("", None))


def test_reports_deletes_is_false():
    # Staging is append-only export history — no upstream deletion to propagate.
    assert OpsEventBridgeIngestor(InMemoryStagingSink()).reports_deletes is False
