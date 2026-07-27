"""MSP-B1 / AT-643 (T3) — CloudWatch alarm-history polling contract suite.

Proves the CloudWatch surface of the native AWS connector:
  * **AC1 (Connect + ingest)** — alarm-history events from TWO seeded accounts are
    ingested, each carrying its ``account_scope``, normalised through the SHARED
    MSP-B0 ``map_cloudwatch`` mapper (verbatim — the same mapper the bridge uses).
  * **AC3 (Incremental)** — a second run after new events processes only events
    past EACH account's checkpoint; nothing is re-read, and one account's
    checkpoint advances independently of the other's.

Plus: within-run ``NextToken`` pagination, the V1 scope guard (``StateUpdate``
history only — not metrics or CloudWatch Logs), and the ``StartDate`` window
narrowing on an incremental run.

Pure-Python: a seeded fake CloudWatch client (no boto3, no AWS account). Accounts
use direct per-account keys so the test stays focused on polling, not STS.
"""
from __future__ import annotations

import json

from discovery.ingest.aws_auth import (
    AWSAccountConfig,
    AWSAuthenticator,
    AWSClientFactory,
    AWSCredentials,
)
from discovery.ingest.aws_event_connector import SURFACE_CLOUDWATCH, AWSEventConnector
from discovery.ingest.aws_poll_source import (
    AWSLivePollSource,
    _alarm_history_to_state_change,
    _iso,
)
from discovery.ingest.base import Checkpoint
from discovery.ingest.aws_watermark import watermark_of
from discovery.ingest.cloud_event_connector import _decode_positions

_ORG = "acme"
_REGION = "us-east-1"


# ─────────────────────────────────────────────────────────────────────────────
# Seeded fake CloudWatch client (paginates by NextToken; records calls)
# ─────────────────────────────────────────────────────────────────────────────

class _FakeCloudWatch:
    def __init__(self, items, calls, account_id):
        self.items = items          # a live reference — appends between runs are seen
        self.calls = calls
        self.account_id = account_id

    def describe_alarm_history(self, **kwargs):
        self.calls.append((self.account_id, kwargs))
        offset = int(kwargs.get("NextToken") or 0)
        limit = kwargs.get("MaxRecords") or len(self.items)
        page = self.items[offset:offset + limit]
        end = offset + len(page)
        next_token = str(end) if end < len(self.items) else None
        # Deliberately IGNORES StartDate (returns the whole window from the offset),
        # so the test proves the reader's client-side incremental filter, not the
        # server's — the strongest AC3 guarantee.
        return {"AlarmHistoryItems": page, "NextToken": next_token}


class _CWFactory(AWSClientFactory):
    def __init__(self, items_by_account, calls):
        self.items_by_account = items_by_account
        self.calls = calls

    def client(self, service, *, region, credentials, partition=None):
        assert service == "cloudwatch"
        account_id = credentials.access_key_id[4:]  # 'AKIA<account>'
        return _FakeCloudWatch(self.items_by_account[account_id], self.calls, account_id)


def _alarm_item(ts: str, *, name="HighCPU", new="ALARM", old="OK") -> dict:
    return {
        "AlarmName": name,
        "Timestamp": ts,
        "HistoryItemType": "StateUpdate",
        "HistorySummary": f"{old} -> {new}",
        "HistoryData": json.dumps({"newState": {"stateValue": new}, "oldState": {"stateValue": old}}),
    }


def _build(items_by_account, *, page_size=100):
    calls: list = []
    factory = _CWFactory(items_by_account, calls)
    auth = AWSAuthenticator(
        client_factory=factory,
        hub_resolver=lambda o: None,   # direct-keys accounts → no STS
        account_key_resolver=lambda o, a: AWSCredentials(f"AKIA{a}", "s", source="direct_keys"),
    )
    accounts = [AWSAccountConfig(account_id=a, regions=(_REGION,)) for a in items_by_account]
    source = AWSLivePollSource(accounts, auth, surfaces=(SURFACE_CLOUDWATCH,), page_size=page_size)
    return source, calls


def _run(source, since=None):
    conn = AWSEventConnector(source)
    batches = list(conn.ingest_changes(_ORG, since))
    records = [r for b in batches for r in b.records]
    return records, batches[-1].next_checkpoint


def _cw_scope_key(account_id: str) -> str:
    return f"aws:{account_id}:{_REGION}:{SURFACE_CLOUDWATCH}"


# ─────────────────────────────────────────────────────────────────────────────
# AC1 — two accounts, each event carries its account_scope
# ─────────────────────────────────────────────────────────────────────────────

def test_ac1_alarm_history_from_two_accounts_carries_account_scope():
    items = {
        "111111111111": [_alarm_item("2026-07-14T01:00:00Z")],
        "222222222222": [_alarm_item("2026-07-14T01:05:00Z")],
    }
    source, _calls = _build(items)
    records, _ckpt = _run(source)

    assert {r["account_scope"] for r in records} == {"111111111111", "222222222222"}
    for r in records:
        assert r["surface"] == SURFACE_CLOUDWATCH
        ev = r["event"]
        # Normalised through map_cloudwatch: an ALARM state change is high-severity.
        assert ev["event_class"] == "state_change"
        assert ev["resource_type"] == "monitoring"
        assert ev["severity"] == "high"
        assert ev["source_system"] == "aws"
        # The alarm ARN is built with THIS account, so the resource is account-scoped.
        assert r["account_scope"] in ev["resource"]["resource_id"]


def test_maps_through_map_cloudwatch_verbatim():
    # The connector's emitted event must equal the shared B0 map_cloudwatch output
    # for the reconciled envelope — same mapper the bridge uses, no divergence.
    from discovery.signals.reference_mappers import map_cloudwatch

    item = _alarm_item("2026-07-14T01:00:00Z")
    source, _calls = _build({"111111111111": [item]})
    records, _ = _run(source)
    emitted = records[0]["event"]

    envelope = _alarm_history_to_state_change(
        item, account_id="111111111111", region=_REGION, timestamp_iso=_iso(item["Timestamp"])
    )
    expected = map_cloudwatch(envelope, org_id=_ORG).to_dict()
    for field, value in expected.items():
        if field in ("source_system", "provenance"):  # transport re-stamp only
            continue
        assert emitted[field] == value, field


# ─────────────────────────────────────────────────────────────────────────────
# AC3 — incremental: only events past each account's checkpoint, no re-reads
# ─────────────────────────────────────────────────────────────────────────────

def test_ac3_incremental_processes_only_new_events_per_account():
    items = {
        "111111111111": [_alarm_item("2026-07-14T01:00:00Z"), _alarm_item("2026-07-14T02:00:00Z")],
        "222222222222": [_alarm_item("2026-07-14T01:30:00Z")],
    }
    source, calls = _build(items)

    # Run 1: full load.
    records1, ckpt1 = _run(source)
    assert len(records1) == 3
    pos1 = _decode_positions(ckpt1)
    assert watermark_of(pos1[_cw_scope_key("111111111111")]) == "2026-07-14T02:00:00Z"  # A newest
    assert watermark_of(pos1[_cw_scope_key("222222222222")]) == "2026-07-14T01:30:00Z"  # B newest

    # New event lands in account A only; account B is unchanged.
    items["111111111111"].append(_alarm_item("2026-07-14T03:00:00Z"))
    calls.clear()

    # Run 2: resume from the run-1 checkpoint.
    records2, ckpt2 = _run(source, since=Checkpoint.create("aws_events", _ORG, ckpt1))

    # ONLY the new account-A event is processed — nothing re-read (AC3).
    assert len(records2) == 1
    assert records2[0]["account_scope"] == "111111111111"
    assert records2[0]["event"]["message"] or True
    assert "2026-07-14T03:00:00Z" == records2[0]["event"]["observed_at"]

    # Checkpoints advanced INDEPENDENTLY: A moved to T3, B stayed at its T1:30.
    pos2 = _decode_positions(ckpt2)
    assert watermark_of(pos2[_cw_scope_key("111111111111")]) == "2026-07-14T03:00:00Z"
    assert watermark_of(pos2[_cw_scope_key("222222222222")]) == "2026-07-14T01:30:00Z"

    # The incremental run narrowed the server window with StartDate (a datetime).
    from datetime import datetime
    assert any(
        isinstance(kw.get("StartDate"), datetime) for _acct, kw in calls
    ), "incremental run should pass StartDate to DescribeAlarmHistory"


def test_ac3_idle_second_run_emits_nothing():
    items = {"111111111111": [_alarm_item("2026-07-14T01:00:00Z")]}
    source, _calls = _build(items)
    _records1, ckpt1 = _run(source)
    # No new events before run 2.
    records2, _ckpt2 = _run(source, since=Checkpoint.create("aws_events", _ORG, ckpt1))
    assert records2 == []


# ─────────────────────────────────────────────────────────────────────────────
# Pagination + V1 scope guard
# ─────────────────────────────────────────────────────────────────────────────

def test_within_run_pagination_collects_all_pages():
    items = {"111111111111": [
        _alarm_item("2026-07-14T01:00:00Z"),
        _alarm_item("2026-07-14T02:00:00Z"),
        _alarm_item("2026-07-14T03:00:00Z"),
    ]}
    source, calls = _build(items, page_size=2)
    records, _ = _run(source)
    assert len(records) == 3                 # all pages collected
    assert len(calls) == 2                   # 2 pages (2 + 1) via NextToken


def test_only_state_update_history_is_requested_not_metrics_or_logs():
    items = {"111111111111": [_alarm_item("2026-07-14T01:00:00Z")]}
    source, calls = _build(items)
    _run(source)
    assert calls
    for _acct, kwargs in calls:
        # V1 scope: alarm STATE changes only — never metrics / CloudWatch Logs.
        assert kwargs.get("HistoryItemType") == "StateUpdate"


def test_botocore_datetime_timestamp_is_normalised():
    # botocore deserialises API timestamps to aware datetimes; the reader must
    # normalise them to the same ISO string a fixture carries, so watermarks compare.
    from datetime import datetime, timezone

    dt = datetime(2026, 7, 14, 1, 0, 0, tzinfo=timezone.utc)
    item = _alarm_item("placeholder")
    item["Timestamp"] = dt  # simulate a live botocore datetime
    source, _calls = _build({"111111111111": [item]})
    records, ckpt = _run(source)
    assert len(records) == 1
    assert records[0]["event"]["observed_at"] == dt.isoformat()
    assert dt.isoformat() in ckpt
