"""MSP-B1 / AT-644 (T4) — CloudTrail + EventBridge polling contract suite.

Proves the CloudTrail (management events) and EventBridge (bounded rule set)
surfaces of the native AWS connector:
  * **AC1 (Connect + ingest)** — bounded EventBridge and CloudTrail management
    events from TWO seeded accounts are ingested, each carrying its
    ``account_scope``, normalised through the SHARED B0 ``map_cloudtrail`` /
    ``map_eventbridge`` mappers (verbatim — the same mappers the bridge uses).
  * **AC2 (Scope defence)** — out-of-scope classes are NOT ingested, verified by
    seeding them: CloudTrail **data** and **Insight** events, and a non-StateUpdate
    CloudWatch history item (a metric/config item). No GuardDuty/Security Hub API
    is ever called (not in scope, not granted).
  * **AC3 (Incremental)** — a second run after new events processes only events
    past each account's checkpoint (no re-reads): CloudTrail by time watermark,
    EventBridge by rule-signature (an unchanged rule set re-reads nothing).

Pure-Python: seeded fake CloudTrail/EventBridge/CloudWatch clients (no boto3, no
AWS account). Accounts use direct per-account keys so the test stays focused on
polling, not STS.
"""
from __future__ import annotations

import json

from discovery.ingest.aws_auth import (
    AWSAccountConfig,
    AWSAuthenticator,
    AWSClientFactory,
    AWSCredentials,
)
from discovery.ingest.aws_event_connector import (
    SURFACE_CLOUDTRAIL,
    SURFACE_CLOUDWATCH,
    SURFACE_EVENTBRIDGE,
    AWSEventConnector,
)
from discovery.ingest.aws_poll_source import AWSLivePollSource
from discovery.ingest.aws_watermark import watermark_of
from discovery.ingest.base import Checkpoint
from discovery.ingest.cloud_event_connector import _decode_positions

_ORG = "acme"
_REGION = "us-east-1"


# ─────────────────────────────────────────────────────────────────────────────
# Seeded fakes (paginate by NextToken; ignore Start* so the reader's client-side
# filter is what's under test)
# ─────────────────────────────────────────────────────────────────────────────

class _FakeCloudTrail:
    def __init__(self, records, calls, account_id):
        self.records = records
        self.calls = calls
        self.account_id = account_id

    def lookup_events(self, **kwargs):
        self.calls.append((self.account_id, "cloudtrail", kwargs))
        offset = int(kwargs.get("NextToken") or 0)
        limit = kwargs.get("MaxResults") or len(self.records)
        page = self.records[offset:offset + limit]
        end = offset + len(page)
        return {
            "Events": [{"CloudTrailEvent": json.dumps(r)} for r in page],
            "NextToken": str(end) if end < len(self.records) else None,
        }


class _FakeEventBridge:
    def __init__(self, rules, calls, account_id):
        self.rules = rules
        self.calls = calls
        self.account_id = account_id

    def list_rules(self, **kwargs):
        self.calls.append((self.account_id, "events", kwargs))
        offset = int(kwargs.get("NextToken") or 0)
        limit = kwargs.get("Limit") or len(self.rules)
        page = self.rules[offset:offset + limit]
        end = offset + len(page)
        return {
            "Rules": [{"Name": r["Name"], "Arn": r["Arn"]} for r in page],
            "NextToken": str(end) if end < len(self.rules) else None,
        }

    def describe_rule(self, *, Name):
        for r in self.rules:
            if r["Name"] == Name:
                return dict(r)
        return {"Name": Name}


class _FakeCloudWatch:
    def __init__(self, items, calls, account_id):
        self.items = items
        self.calls = calls
        self.account_id = account_id

    def describe_alarm_history(self, **kwargs):
        self.calls.append((self.account_id, "cloudwatch", kwargs))
        return {"AlarmHistoryItems": list(self.items), "NextToken": None}


class _AWSFakeFactory(AWSClientFactory):
    def __init__(self, data_by_account):
        self.data_by_account = data_by_account
        self.calls: list = []
        self.services_seen: set = set()

    def client(self, service, *, region, credentials, partition=None):
        self.services_seen.add(service)
        account_id = credentials.access_key_id[4:]  # 'AKIA<account>'
        data = self.data_by_account.get(account_id, {})
        if service == "cloudtrail":
            return _FakeCloudTrail(data.get("cloudtrail", []), self.calls, account_id)
        if service == "events":
            return _FakeEventBridge(data.get("eventbridge", []), self.calls, account_id)
        if service == "cloudwatch":
            return _FakeCloudWatch(data.get("cloudwatch", []), self.calls, account_id)
        raise AssertionError(f"unexpected service {service!r}")


def _mgmt_event(event_id, ts, *, name="AssumeRole", source="sts.amazonaws.com"):
    return {
        "eventID": event_id, "eventTime": ts, "eventSource": source, "eventName": name,
        "eventCategory": "Management", "managementEvent": True,
        "userIdentity": {"type": "IAMUser", "arn": "arn:aws:iam::111111111111:user/alice"},
        "resources": [{"ARN": "arn:aws:iam::111111111111:role/admin"}],
    }


def _rule(name, account_id, *, state="ENABLED", pattern=""):
    return {
        "Name": name,
        "Arn": f"arn:aws:events:{_REGION}:{account_id}:rule/{name}",
        "State": state,
        "EventPattern": pattern,
    }


def _build(data_by_account, surfaces, *, page_size=100):
    factory = _AWSFakeFactory(data_by_account)
    auth = AWSAuthenticator(
        client_factory=factory,
        hub_resolver=lambda o: None,
        account_key_resolver=lambda o, a: AWSCredentials(f"AKIA{a}", "s", source="direct_keys"),
    )
    accounts = [AWSAccountConfig(account_id=a, regions=(_REGION,)) for a in data_by_account]
    source = AWSLivePollSource(accounts, auth, surfaces=surfaces, page_size=page_size)
    return source, factory


def _run(source, since=None):
    conn = AWSEventConnector(source)
    batches = list(conn.ingest_changes(_ORG, since))
    records = [r for b in batches for r in b.records]
    return records, batches[-1].next_checkpoint


def _scope_key(account_id, surface):
    return f"aws:{account_id}:{_REGION}:{surface}"


# ─────────────────────────────────────────────────────────────────────────────
# AC1 — CloudTrail management + EventBridge from two accounts, account-scoped
# ─────────────────────────────────────────────────────────────────────────────

def test_ac1_cloudtrail_and_eventbridge_from_two_accounts_carry_account_scope():
    data = {
        "111111111111": {
            "cloudtrail": [_mgmt_event("ct-A", "2026-07-14T03:00:00Z")],
            "eventbridge": [_rule("r-A", "111111111111")],
        },
        "222222222222": {
            "cloudtrail": [_mgmt_event("ct-B", "2026-07-14T03:05:00Z")],
            "eventbridge": [_rule("r-B", "222222222222")],
        },
    }
    source, _factory = _build(data, (SURFACE_CLOUDTRAIL, SURFACE_EVENTBRIDGE))
    records, _ = _run(source)

    assert {r["account_scope"] for r in records} == {"111111111111", "222222222222"}
    for account_id in ("111111111111", "222222222222"):
        acct = [r for r in records if r["account_scope"] == account_id]
        surfaces = {r["surface"] for r in acct}
        assert surfaces == {SURFACE_CLOUDTRAIL, SURFACE_EVENTBRIDGE}
        ct = next(r for r in acct if r["surface"] == SURFACE_CLOUDTRAIL)["event"]
        assert ct["event_class"] == "access"      # AssumeRole → access (map_cloudtrail)
        assert ct["source_system"] == "aws"
        eb = next(r for r in acct if r["surface"] == SURFACE_EVENTBRIDGE)["event"]
        assert eb["resource_type"] == "messaging"  # events service (map_eventbridge)


def test_maps_through_b0_mappers_verbatim():
    from discovery.signals.reference_mappers import map_cloudtrail

    rec = _mgmt_event("ct-A", "2026-07-14T03:00:00Z")
    source, _factory = _build(
        {"111111111111": {"cloudtrail": [rec]}}, (SURFACE_CLOUDTRAIL,)
    )
    records, _ = _run(source)
    emitted = records[0]["event"]
    expected = map_cloudtrail(rec, org_id=_ORG).to_dict()
    for field, value in expected.items():
        if field in ("source_system", "provenance"):
            continue
        assert emitted[field] == value, field


# ─────────────────────────────────────────────────────────────────────────────
# AC2 — scope defence: out-of-scope classes are not ingested (seeded)
# ─────────────────────────────────────────────────────────────────────────────

def test_ac2_out_of_scope_classes_are_not_ingested():
    data = {
        "111111111111": {
            "cloudtrail": [
                _mgmt_event("ct-mgmt", "2026-07-14T03:00:00Z"),                    # in scope
                {"eventID": "ct-data", "eventTime": "2026-07-14T03:01:00Z",         # DATA event
                 "eventName": "GetObject", "eventSource": "s3.amazonaws.com",
                 "eventCategory": "Data", "managementEvent": False},
                {"eventID": "ct-insight", "eventTime": "2026-07-14T03:02:00Z",      # INSIGHT event
                 "eventName": "InsightThing", "eventCategory": "Insight"},
            ],
            "cloudwatch": [
                {"AlarmName": "HighCPU", "Timestamp": "2026-07-14T01:00:00Z",       # in scope
                 "HistoryItemType": "StateUpdate", "HistorySummary": "OK -> ALARM",
                 "HistoryData": json.dumps({"newState": {"stateValue": "ALARM"}})},
                {"AlarmName": "HighCPU", "Timestamp": "2026-07-14T01:05:00Z",       # NOT a state change
                 "HistoryItemType": "ConfigurationUpdate", "HistorySummary": "cfg"},
            ],
            "eventbridge": [_rule("r-A", "111111111111")],                          # in scope
        }
    }
    source, factory = _build(
        data, (SURFACE_CLOUDWATCH, SURFACE_EVENTBRIDGE, SURFACE_CLOUDTRAIL)
    )
    records, _ = _run(source)
    ingested_ids = {r["provider_event_id"] for r in records}

    # In-scope classes are present...
    assert "ct-mgmt" in ingested_ids
    assert any(r["surface"] == SURFACE_EVENTBRIDGE for r in records)
    assert any(r["surface"] == SURFACE_CLOUDWATCH for r in records)
    # ...out-of-scope classes are NOT.
    assert "ct-data" not in ingested_ids           # CloudTrail data event
    assert "ct-insight" not in ingested_ids         # CloudTrail Insight event
    cw = [r for r in records if r["surface"] == SURFACE_CLOUDWATCH]
    assert len(cw) == 1                             # only the StateUpdate, not the config item

    # No GuardDuty / Security Hub / logs / metrics APIs are ever called.
    assert factory.services_seen <= {"cloudwatch", "events", "cloudtrail"}


# ─────────────────────────────────────────────────────────────────────────────
# AC3 — incremental (no re-reads) for CloudTrail (time) and EventBridge (content)
# ─────────────────────────────────────────────────────────────────────────────

def test_ac3_cloudtrail_incremental_per_account():
    data = {
        "111111111111": {"cloudtrail": [
            _mgmt_event("ct-A1", "2026-07-14T03:00:00Z"),
            _mgmt_event("ct-A2", "2026-07-14T04:00:00Z"),
        ]},
        "222222222222": {"cloudtrail": [_mgmt_event("ct-B1", "2026-07-14T03:30:00Z")]},
    }
    source, _factory = _build(data, (SURFACE_CLOUDTRAIL,))

    records1, ckpt1 = _run(source)
    assert len(records1) == 3

    data["111111111111"]["cloudtrail"].append(_mgmt_event("ct-A3", "2026-07-14T05:00:00Z"))
    records2, ckpt2 = _run(source, since=Checkpoint.create("aws_events", _ORG, ckpt1))

    assert [r["provider_event_id"] for r in records2] == ["ct-A3"]  # only the new one
    pos2 = _decode_positions(ckpt2)
    assert watermark_of(pos2[_scope_key("111111111111", SURFACE_CLOUDTRAIL)]) == "2026-07-14T05:00:00Z"
    assert watermark_of(pos2[_scope_key("222222222222", SURFACE_CLOUDTRAIL)]) == "2026-07-14T03:30:00Z"  # unchanged


def test_ac3_eventbridge_unchanged_ruleset_is_not_re_read():
    data = {"111111111111": {"eventbridge": [_rule("r1", "111111111111")]}}
    source, _factory = _build(data, (SURFACE_EVENTBRIDGE,))

    records1, ckpt1 = _run(source)
    assert len(records1) == 1                       # r1 is new → emitted

    # Second run, rule set unchanged → nothing re-read.
    records2, ckpt2 = _run(source, since=Checkpoint.create("aws_events", _ORG, ckpt1))
    assert records2 == []

    # A NEW rule appears → only it is emitted (event_type carries the rule name).
    data["111111111111"]["eventbridge"].append(_rule("r2", "111111111111"))
    records3, ckpt3 = _run(source, since=Checkpoint.create("aws_events", _ORG, ckpt2))
    assert [r["event"]["event_type"] for r in records3] == ["r2"]

    # A CHANGED rule (state flip) re-emits.
    data["111111111111"]["eventbridge"][0]["State"] = "DISABLED"
    records4, _ckpt4 = _run(source, since=Checkpoint.create("aws_events", _ORG, ckpt3))
    assert [r["event"]["event_type"] for r in records4] == ["r1"]
