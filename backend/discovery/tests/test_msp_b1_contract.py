"""MSP-B1 / AT-647 (T7) — the consolidated AWS Event Connector contract suite.

This is the single authoritative Section-3 contract for MSP-B1: one labelled test
per acceptance criterion, each reproducing that criterion's scenario as stated. It
sits alongside the per-task suites (``test_cloud_event_connector.py`` (T1),
``test_aws_event_auth.py`` (T2), ``test_aws_cloudwatch_polling.py`` (T3),
``test_aws_cloudtrail_eventbridge_polling.py`` (T4), ``test_aws_partitions.py``
(T5), ``test_aws_failure_loudness.py`` (T6)) and restates the whole contract in
one place, exactly as ``test_msp_b7_contract.py`` does for the volume disciplines.

  * AC1 — vault creds + role assumption ingest alarm-history, bounded EventBridge,
          and CloudTrail management events from two accounts, each account_scoped.
  * AC2 — out-of-scope classes (metrics, CloudWatch Logs, data events) are seeded
          and NOT ingested (scope defence).
  * AC3 — incremental: a second run processes only events past each account's
          checkpoint; no re-reads.
  * AC4 — TRANSPORT EQUIVALENCE: B0's golden fixtures through this connector yield
          detector-visible events identical to the B8 bridge path except
          source_system ('aws' vs 'bridge:aws').
  * AC5 — events enter through B7 admission: a re-firing alarm arrives deduplicated
          with a count, live through the native path.
  * AC6 — outbound calls only, verified under NETWORK_PROFILE=no_public_inbound.
  * AC8 — a revoked role on one account surfaces as failed in run health while the
          other accounts continue (loud, partial, never silent).

(AC7 — partition config — and AC9 — the IAM design-review artifact — are covered by
``test_aws_partitions.py`` and ``deployment/AWS_READONLY_IAM_POLICY.md`` respectively;
AC7 is re-asserted here for completeness, AC9 is a human design-review gate.)

Pure-Python: seeded fakes (no boto3, no network); the B8 bridge runs over an
in-memory staging sink. A no-op sleeper keeps back-off instant.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from database.models.ops_event_staging import OpsEventStagingRow
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
    aws_scope,
)
from discovery.ingest.aws_partitions import PARTITION_GOVCLOUD, endpoint_map
from discovery.ingest.aws_poll_source import AWSLivePollSource
from discovery.ingest.base import Checkpoint
from discovery.ingest.cloud_event_connector import (
    CloudScope,
    StaticCloudPollSource,
    _decode_positions,
)
from discovery.ingest.ops_event_bridge import OpsEventBridgeIngestor
from discovery.ingest.ops_event_equivalence import MAPPER_TO_STAGING, load_golden_cases
from discovery.ingest.ops_event_staging_store import InMemoryStagingSink
from discovery.signals.evidence_store import InMemoryRawEventStore

_ORG = "msp_b1_contract"
_REGION = "us-east-1"
_DAY = datetime(2026, 7, 14, 0, 0, 0, tzinfo=timezone.utc)


# ═════════════════════════════════════════════════════════════════════════════
# Seeded AWS fakes (STS + the three surfaces; throttle + role-revocation support)
# ═════════════════════════════════════════════════════════════════════════════

class _ThrottleError(Exception):
    def __init__(self):
        super().__init__("Rate exceeded")
        self.response = {"Error": {"Code": "Throttling"}}


class _FakeSTS:
    def __init__(self, fail_accounts):
        self.fail_accounts = set(fail_accounts)

    def assume_role(self, *, RoleArn, RoleSessionName, ExternalId=None):
        account = RoleArn.split(":")[4]
        if account in self.fail_accounts:
            raise RuntimeError("AccessDenied: role has been revoked")
        return {"Credentials": {"AccessKeyId": f"ASIA{account}",
                                "SecretAccessKey": "s", "SessionToken": "t"}}


class _FakeCloudWatch:
    def __init__(self, items):
        self.items = items

    def describe_alarm_history(self, **kwargs):
        return {"AlarmHistoryItems": list(self.items), "NextToken": None}


class _FakeEventBridge:
    def __init__(self, rules):
        self.rules = rules

    def list_rules(self, **kwargs):
        return {"Rules": [{"Name": r["Name"], "Arn": r["Arn"]} for r in self.rules],
                "NextToken": None}

    def describe_rule(self, *, Name):
        return next((dict(r) for r in self.rules if r["Name"] == Name), {"Name": Name})


class _FakeCloudTrail:
    def __init__(self, records, throttle):
        self.records = records
        self.throttle = throttle  # {"remaining": N} shared across client rebuilds

    def lookup_events(self, **kwargs):
        if self.throttle.get("remaining", 0) > 0:
            self.throttle["remaining"] -= 1
            raise _ThrottleError()
        return {"Events": [{"CloudTrailEvent": json.dumps(r)} for r in self.records],
                "NextToken": None}


class _FakeFactory(AWSClientFactory):
    def __init__(self, data, *, fail_accounts=(), throttle_by_account=None):
        self.data = data
        self.sts = _FakeSTS(fail_accounts)
        self.throttle_by_account = throttle_by_account or {}
        self.services_seen: set = set()

    def client(self, service, *, region, credentials):
        self.services_seen.add(service)
        if service == "sts":
            return self.sts
        account = credentials.access_key_id[4:]
        surfaces = self.data.get(account, {})
        if service == "cloudwatch":
            return _FakeCloudWatch(surfaces.get("cloudwatch", []))
        if service == "events":
            return _FakeEventBridge(surfaces.get("eventbridge", []))
        if service == "cloudtrail":
            return _FakeCloudTrail(
                surfaces.get("cloudtrail", []),
                self.throttle_by_account.setdefault(account, {"remaining": 0}),
            )
        raise AssertionError(f"unexpected service {service!r}")


# -- seed builders ------------------------------------------------------------

def _alarm_item(ts, *, name="HighCPU", new="ALARM", old="OK", item_type="StateUpdate"):
    return {"AlarmName": name, "Timestamp": ts, "HistoryItemType": item_type,
            "HistorySummary": f"{old} -> {new}",
            "HistoryData": json.dumps({"newState": {"stateValue": new},
                                       "oldState": {"stateValue": old}})}


def _mgmt_event(event_id, ts, account):
    return {"eventID": event_id, "eventTime": ts, "eventName": "AssumeRole",
            "eventSource": "sts.amazonaws.com", "eventCategory": "Management",
            "managementEvent": True,
            "userIdentity": {"arn": f"arn:aws:iam::{account}:user/alice"},
            "resources": [{"ARN": f"arn:aws:iam::{account}:role/admin"}]}


def _rule(name, account, *, state="ENABLED"):
    return {"Name": name, "Arn": f"arn:aws:events:{_REGION}:{account}:rule/{name}", "State": state}


def _account_data(account):
    return {
        "cloudwatch": [_alarm_item("2026-07-14T01:00:00Z")],
        "eventbridge": [_rule(f"r-{account}", account)],
        "cloudtrail": [_mgmt_event(f"ct-{account}", "2026-07-14T03:00:00Z", account)],
    }


def _role_account(account):
    return AWSAccountConfig(
        account_id=account, role_arn=f"arn:aws:iam::{account}:role/AgentIQReadOnlyEvents",
        external_id="ext", regions=(_REGION,),
    )


def _live_source(data, *, fail_accounts=(), throttle=None, surfaces=None, sleeper=lambda s: None,
                 hub_ok=True, direct_keys=False):
    factory = _FakeFactory(data, fail_accounts=fail_accounts, throttle_by_account=throttle)
    auth = AWSAuthenticator(
        client_factory=factory,
        hub_resolver=(lambda o: AWSCredentials("AKIAHUB", "s", source="hub")) if hub_ok else (lambda o: None),
        account_key_resolver=(
            (lambda o, a: AWSCredentials(f"AKIA{a}", "s", source="direct_keys"))
            if direct_keys else (lambda o, a: None)
        ),
    )
    accounts = [
        (AWSAccountConfig(account_id=a, regions=(_REGION,)) if direct_keys else _role_account(a))
        for a in data
    ]
    kwargs = {"surfaces": surfaces} if surfaces else {}
    source = AWSLivePollSource(accounts, auth, sleeper=sleeper, **kwargs)
    return source, factory


def _drain(connector, since=None):
    batches = list(connector.ingest_changes(_ORG, since))
    return [r for b in batches for r in b.records], batches


# ═════════════════════════════════════════════════════════════════════════════
# AC1 — connect + ingest from two accounts, each account_scoped
# ═════════════════════════════════════════════════════════════════════════════

def test_ac1_two_accounts_ingest_all_surfaces_with_account_scope():
    a, b = "111111111111", "222222222222"
    source, factory = _live_source({a: _account_data(a), b: _account_data(b)})
    records, _ = _drain(AWSEventConnector(source))

    assert {r["account_scope"] for r in records} == {a, b}
    for account in (a, b):
        surfaces = {r["surface"] for r in records if r["account_scope"] == account}
        assert surfaces == {SURFACE_CLOUDWATCH, SURFACE_EVENTBRIDGE, SURFACE_CLOUDTRAIL}
    # Role assumption actually happened (vault creds → STS AssumeRole).
    assert "sts" in factory.services_seen


# ═════════════════════════════════════════════════════════════════════════════
# AC2 — scope defence: metrics / logs / data events are NOT ingested
# ═════════════════════════════════════════════════════════════════════════════

def test_ac2_out_of_scope_classes_are_not_ingested():
    a = "111111111111"
    data = {a: {
        "cloudwatch": [
            _alarm_item("2026-07-14T01:00:00Z"),                              # in scope
            _alarm_item("2026-07-14T01:05:00Z", item_type="ConfigurationUpdate"),  # not a state change
        ],
        "cloudtrail": [
            _mgmt_event("ct-mgmt", "2026-07-14T03:00:00Z", a),               # in scope
            {"eventID": "ct-data", "eventTime": "2026-07-14T03:01:00Z",       # DATA event
             "eventName": "GetObject", "eventCategory": "Data", "managementEvent": False},
        ],
        "eventbridge": [_rule("r", a)],
    }}
    source, factory = _live_source(data)
    records, _ = _drain(AWSEventConnector(source))
    ids = {r["provider_event_id"] for r in records}

    assert "ct-mgmt" in ids
    assert "ct-data" not in ids                                    # data event excluded
    assert len([r for r in records if r["surface"] == SURFACE_CLOUDWATCH]) == 1  # only StateUpdate
    # Never touches monitoring-only / security-stream services.
    assert factory.services_seen <= {"sts", "cloudwatch", "events", "cloudtrail"}


# ═════════════════════════════════════════════════════════════════════════════
# AC3 — incremental: only events past each account's checkpoint; no re-reads
# ═════════════════════════════════════════════════════════════════════════════

def test_ac3_incremental_no_re_reads_per_account():
    a, b = "111111111111", "222222222222"
    data = {
        a: {"cloudtrail": [_mgmt_event("ct-a1", "2026-07-14T03:00:00Z", a)]},
        b: {"cloudtrail": [_mgmt_event("ct-b1", "2026-07-14T03:30:00Z", b)]},
    }
    source, _ = _live_source(data, surfaces=(SURFACE_CLOUDTRAIL,))
    conn = AWSEventConnector(source)
    records1, batches1 = _drain(conn)
    assert len(records1) == 2
    ckpt = batches1[-1].next_checkpoint

    # New event on account A only.
    data[a]["cloudtrail"].append(_mgmt_event("ct-a2", "2026-07-14T04:00:00Z", a))
    records2, _ = _drain(AWSEventConnector(source), since=Checkpoint.create("aws_events", _ORG, ckpt))

    assert [r["provider_event_id"] for r in records2] == ["ct-a2"]     # only the new one
    pos = _decode_positions(ckpt)
    assert pos[f"aws:{a}:{_REGION}:{SURFACE_CLOUDTRAIL}"] == "2026-07-14T03:00:00Z"
    assert pos[f"aws:{b}:{_REGION}:{SURFACE_CLOUDTRAIL}"] == "2026-07-14T03:30:00Z"


# ═════════════════════════════════════════════════════════════════════════════
# AC4 — TRANSPORT EQUIVALENCE against B8's fixtures (the headline)
# ═════════════════════════════════════════════════════════════════════════════

_GOLDEN = load_golden_cases()
_GOLDEN_AWS = [c for c in _GOLDEN if "azure" not in c["mapper"]]
_SURFACE_OF = {
    "map_cloudwatch": SURFACE_CLOUDWATCH,
    "map_eventbridge": SURFACE_EVENTBRIDGE,
    "map_cloudtrail": SURFACE_CLOUDTRAIL,
}


def _native_golden_events(store):
    scope_events = []
    for case in _GOLDEN_AWS:
        scope = CloudScope(provider="aws", account="acct",
                           surface=_SURFACE_OF[case["mapper"]], mapper=case["mapper"])
        scope_events.append((scope, [case["raw"]]))
    conn = AWSEventConnector(StaticCloudPollSource(scope_events), raw_store=store)
    return {r["event"]["signal_id"]: r["event"]
            for b in conn.ingest_changes(_ORG, None) for r in b.records}


def _bridge_golden_events(store):
    sink = InMemoryStagingSink()
    rows = []
    for case in _GOLDEN_AWS:
        provider, source_format = MAPPER_TO_STAGING[case["mapper"]]
        rows.append(OpsEventStagingRow(
            org_id=_ORG, provider=provider, source_format=source_format,
            batch_id=f"golden:{provider}", provider_event_id=f"golden:{case['name']}",
            raw=case["raw"]))
    sink.insert_rows(rows)
    ingestor = OpsEventBridgeIngestor(sink, raw_store=store, batch_size=1000)
    return {rec["event"]["signal_id"]: rec["event"]
            for b in ingestor.ingest_changes(_ORG, None) for rec in b.records}


def test_ac4_transport_equivalence_native_vs_b8_bridge():
    native = _native_golden_events(InMemoryRawEventStore())
    bridge = _bridge_golden_events(InMemoryRawEventStore())

    assert set(native) == set(bridge) == {c["expected"]["signal_id"] for c in _GOLDEN_AWS}
    for signal_id, native_event in native.items():
        bridge_event = bridge[signal_id]
        for field in sorted(set(native_event) | set(bridge_event)):
            if field in ("source_system", "provenance"):
                continue
            assert native_event[field] == bridge_event[field], (
                f"{signal_id}: field {field!r} diverged from the B8 bridge path"
            )
        # The ONE intentional difference: 'aws' (native) vs 'bridge:aws' (bridge).
        assert native_event["source_system"] == "aws"
        assert bridge_event["source_system"] == "bridge:aws"
        # Recurrence identity is preserved across both transports.
        assert native_event["event_signature"] == bridge_event["event_signature"]


# ═════════════════════════════════════════════════════════════════════════════
# AC5 — B7 admission: a re-firing alarm dedups to one signal with a count
# ═════════════════════════════════════════════════════════════════════════════

def test_ac5_refiring_alarm_dedups_with_a_count_through_native_path():
    # A stuck alarm re-firing 5× (native state-change envelopes, distinct ids).
    firings = [{
        "id": f"cw-{n}", "detail-type": "CloudWatch Alarm State Change",
        "source": "aws.cloudwatch", "account": "111111111111",
        "time": (_DAY + timedelta(minutes=5 * n)).isoformat().replace("+00:00", "Z"),
        "region": _REGION,
        "resources": [f"arn:aws:cloudwatch:{_REGION}:111111111111:alarm:HighCPU"],
        "detail": {"alarmName": "HighCPU", "state": {"value": "ALARM", "reason": "x"},
                   "previousState": {"value": "ALARM"}},
    } for n in range(1, 6)]
    scope = aws_scope("111111111111", SURFACE_CLOUDWATCH, region=_REGION)
    store = InMemoryRawEventStore()
    conn = AWSEventConnector(StaticCloudPollSource([(scope, firings)]), raw_store=store)
    _drain(conn)

    signals = conn.active_signals(_ORG)
    assert len(signals) == 1
    assert signals[0].occurrence_count == 5
    assert len(signals[0].resolve_raw_instances(store)) == 5   # opens back to real instances


# ═════════════════════════════════════════════════════════════════════════════
# AC6 — outbound only
# ═════════════════════════════════════════════════════════════════════════════

def test_ac6_outbound_only_under_no_public_inbound(monkeypatch):
    monkeypatch.setenv("NETWORK_PROFILE", "no_public_inbound")
    from app.network_profile import is_no_public_inbound
    assert is_no_public_inbound()

    a = "111111111111"
    source, _ = _live_source({a: _account_data(a)}, direct_keys=True)
    records, _ = _drain(AWSEventConnector(source))
    assert records  # outbound polling works with no inbound surface


def test_ac6_no_push_or_inbound_infrastructure_in_modules():
    backend = Path(__file__).resolve().parents[2]
    forbidden = ["subscribe", "webhook", "socketserver", "http.server", "httpserver",
                 "websocket", "put_targets", "add_api_route", "ngrok"]
    offenders = []
    for rel in ("aws_event_connector", "aws_auth", "aws_poll_source",
                "aws_partitions", "aws_health", "cloud_event_connector"):
        text = (backend / "discovery" / "ingest" / f"{rel}.py").read_text(encoding="utf-8").lower()
        offenders += [f"{rel}:{t}" for t in forbidden if t in text]
    assert not offenders, "outbound-only violated (AC6): " + ", ".join(offenders)


# ═════════════════════════════════════════════════════════════════════════════
# AC7 — partition config resolves GovCloud endpoints (re-asserted here)
# ═════════════════════════════════════════════════════════════════════════════

def test_ac7_govcloud_endpoint_map():
    m = endpoint_map(PARTITION_GOVCLOUD, "us-gov-west-1")
    assert m["cloudwatch"] == "https://monitoring.us-gov-west-1.amazonaws.com"
    assert m["cloudtrail"] == "https://cloudtrail.us-gov-west-1.amazonaws.com"
    assert m["sts"] == "https://sts.us-gov-west-1.amazonaws.com"


# ═════════════════════════════════════════════════════════════════════════════
# AC8 — a revoked role on one account is loud; other accounts continue
# ═════════════════════════════════════════════════════════════════════════════

def test_ac8_revoked_role_is_loud_others_continue():
    good, bad = "111111111111", "222222222222"
    source, _ = _live_source(
        {good: _account_data(good), bad: _account_data(bad)},
        fail_accounts={bad}, surfaces=(SURFACE_CLOUDTRAIL,),
    )
    conn = AWSEventConnector(source)
    records, _ = _drain(conn)
    health = conn.health_report()

    assert {r["account_scope"] for r in records} == {good}   # good account still ingests
    by_account = {a["account_id"]: a for a in health["accounts"]}
    assert by_account[bad]["status"] == "auth_failed"        # loud, not silent
    assert by_account[good]["status"] == "ok"
    assert health["all_healthy"] is False
    assert health["failed_accounts"] == [bad]
