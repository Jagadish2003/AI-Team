"""MSP-B1 — regression suite for the post-implementation gap review.

The MSP-B1 acceptance criteria were proven by ``test_msp_b1_contract.py`` against
seeded fakes, but a review of the delivered connector found that nothing ever ran
it in a real discovery run, nothing fed it per-org configuration, and several
correctness defects would only surface on the first live poll. This suite pins the
fixes so they cannot regress. One labelled section per gap.

  * G1 — the connector is invoked by a discovery run.
  * G2 — per-org config resolution: the Owner-pinned accounts are read back.
  * G3 — the STS ExternalId survives from the pin flow to the AssumeRole call.
  * G4 — a backlog larger than one poll is drained, never silently truncated.
  * G5 — the EventBridge surface's boundary is declared, and it stays in V1 scope.
  * G6 — per-account health reaches run health.
  * G7 — GovCloud endpoints come from the CONFIGURED partition, not the region.
  * G8 — timestamps compare as instants; same-instant stragglers are not dropped.
  * G9 — the offline fixture clears the B7 noise floors (see also
          ``test_cloud_event_connector.py``).
  * G10 — the hub-credential env fallback is off in production.

Pure-Python: seeded fakes, no boto3, no AWS account, no database.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from discovery.ingest import aws_auth, aws_poll_source
from discovery.ingest.aws_auth import (
    AWSAccountConfig,
    AWSAuthenticator,
    AWSClientFactory,
    AWSCredentials,
    Boto3ClientFactory,
)
from discovery.ingest.aws_event_connector import (
    AWS_SURFACES,
    DEFAULT_POLL_SURFACES,
    SURFACE_CLOUDTRAIL,
    SURFACE_CLOUDWATCH,
    SURFACE_EVENTBRIDGE,
    AWSEventConnector,
    build_ingestor,
)
from discovery.ingest.aws_events_config import (
    AWSEventConfigError,
    config_from_connector_record,
    resolve_aws_event_config,
)
from discovery.ingest.aws_partitions import (
    PARTITION_AWS,
    PARTITION_GOVCLOUD,
    resolve_service_endpoint_or_none,
)
from discovery.ingest.aws_poll_source import AWSLivePollSource
from discovery.ingest.aws_watermark import (
    TimePosition,
    decode_position,
    encode_position,
    watermark_of,
)
from discovery.ingest.base import Checkpoint

_ORG = "gapfix-org"
_REGION = "us-east-1"
_ACCOUNT = "111122223333"
_BASE = datetime(2026, 7, 14, 3, 0, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _mgmt_event(event_id: str, ts: str) -> dict:
    return {
        "eventID": event_id, "eventTime": ts, "eventName": "AssumeRole",
        "eventSource": "sts.amazonaws.com", "eventCategory": "Management",
        "managementEvent": True,
        "userIdentity": {"type": "IAMUser", "arn": f"arn:aws:iam::{_ACCOUNT}:user/alice"},
        "resources": [{"ARN": f"arn:aws:iam::{_ACCOUNT}:role/admin"}],
    }


def _alarm_item(ts: str, *, name: str = "HighCPU") -> dict:
    return {
        "AlarmName": name, "Timestamp": ts, "HistoryItemType": "StateUpdate",
        "HistorySummary": "OK -> ALARM",
        "HistoryData": json.dumps({"newState": {"stateValue": "ALARM"},
                                   "oldState": {"stateValue": "OK"}}),
    }


# ═════════════════════════════════════════════════════════════════════════════
# Fakes that honour the real API contracts (ordering + time windows), because the
# truncation and boundary defects only reproduce against faithful ordering.
# ═════════════════════════════════════════════════════════════════════════════

class _OrderedCloudTrail:
    """LookupEvents: NEWEST-FIRST, StartTime/EndTime inclusive, NextToken paging."""

    def __init__(self, records):
        self.records = records
        self.calls = []

    def lookup_events(self, **kwargs):
        self.calls.append(kwargs)
        start, end = kwargs.get("StartTime"), kwargs.get("EndTime")
        rows = []
        for record in self.records:
            ts = datetime.fromisoformat(record["eventTime"].replace("Z", "+00:00"))
            if start is not None and ts < start:
                continue
            if end is not None and ts > end:
                continue
            rows.append(record)
        rows.sort(key=lambda r: r["eventTime"], reverse=True)   # newest-first
        offset = int(kwargs.get("NextToken") or 0)
        limit = kwargs.get("MaxResults") or len(rows)
        page = rows[offset:offset + limit]
        nxt = offset + len(page)
        return {
            "Events": [{"CloudTrailEvent": json.dumps(r)} for r in page],
            "NextToken": str(nxt) if nxt < len(rows) else None,
        }


class _OrderedCloudWatch:
    """DescribeAlarmHistory: honours ScanBy + StartDate, NextToken paging."""

    def __init__(self, items):
        self.items = items
        self.calls = []

    def describe_alarm_history(self, **kwargs):
        self.calls.append(kwargs)
        start = kwargs.get("StartDate")
        rows = []
        for item in self.items:
            ts = datetime.fromisoformat(item["Timestamp"].replace("Z", "+00:00"))
            if start is not None and ts < start:
                continue
            rows.append(item)
        ascending = kwargs.get("ScanBy") == "TimestampAscending"
        rows.sort(key=lambda r: r["Timestamp"], reverse=not ascending)
        offset = int(kwargs.get("NextToken") or 0)
        limit = kwargs.get("MaxRecords") or len(rows)
        page = rows[offset:offset + limit]
        nxt = offset + len(page)
        return {
            "AlarmHistoryItems": page,
            "NextToken": str(nxt) if nxt < len(rows) else None,
        }


class _Factory(AWSClientFactory):
    def __init__(self, *, cloudtrail=None, cloudwatch=None):
        self.cloudtrail = cloudtrail
        self.cloudwatch = cloudwatch
        self.partitions_seen = []

    def client(self, service, *, region, credentials, partition=None):
        self.partitions_seen.append((service, region, partition))
        if service == "cloudtrail":
            return self.cloudtrail
        if service == "cloudwatch":
            return self.cloudwatch
        raise AssertionError(f"unexpected service {service!r}")


def _source(factory, *, surfaces, page_size=100, account=None):
    auth = AWSAuthenticator(
        client_factory=factory,
        hub_resolver=lambda o: None,
        account_key_resolver=lambda o, a: AWSCredentials(f"AKIA{a}", "s", source="direct_keys"),
    )
    accounts = [account or AWSAccountConfig(account_id=_ACCOUNT, regions=(_REGION,))]
    return AWSLivePollSource(accounts, auth, surfaces=surfaces, page_size=page_size)


def _drain(source, since=None):
    """Drive one full run over ``source``, with the per-run poll bound disabled.

    These tests exercise the READER/watermark semantics (ordering, truncation,
    boundary instants) under a deliberately tiny page budget, so a single logical
    backlog is spread over many polls. The per-run continuation bound the skeleton
    applies in production (poll cap / deadline / B7 budget — see
    ``test_msp_b1_poll_bounds.py``) is a different concern and is switched off here so
    it cannot mask a genuine reader defect.
    """
    conn = AWSEventConnector(source, max_polls_per_scope=0, poll_deadline_seconds=0)
    batches = list(conn.ingest_changes(_ORG, since))
    return [r for b in batches for r in b.records], batches[-1].next_checkpoint


# ═════════════════════════════════════════════════════════════════════════════
# G1 — the connector is actually invoked by a discovery run
# ═════════════════════════════════════════════════════════════════════════════

def test_g1_runner_exposes_an_aws_ingest_stage():
    """The runner must have an AWS stage wired the same way Azure's is.

    Before the fix ``discovery/runner.py`` drove the B8 bridge and the native Azure
    connector but never the native AWS connector, so a real discovery run ingested
    zero AWS events no matter how the connector was configured.
    """
    import inspect

    from discovery import runner

    assert hasattr(runner, "_ingest_aws_events")
    body = inspect.getsource(runner.run)
    assert '"aws_events" in _systems' in body, "AWS stage is not gated into the run"
    assert "_ingest_aws_events(org_id, run_id)" in body, "AWS stage is never called"
    # Its records must reach the SAME cloud-ops assembly seam as the bridge/Azure,
    # or the events are ingested and then dropped on the floor.
    assert 'aws_events_data.get("records")' in body


def test_g1_offline_mode_builds_a_working_connector(monkeypatch):
    """Offline mode must produce a usable connector with no config and no account.

    ``INGEST_MODE`` is set explicitly because importing some app modules mutates it
    as a side effect, which would otherwise make this test order-dependent.
    """
    monkeypatch.setenv("INGEST_MODE", "offline")
    connector = build_ingestor(_ORG)
    assert connector is not None
    records = [r for b in connector.ingest_changes(_ORG, None) for r in b.records]
    assert records, "offline mode produced no AWS events"
    assert {r["source_system"] for r in records} == {"aws"}


def test_g1_live_mode_without_config_contributes_nothing(monkeypatch):
    """An unconfigured live org yields None — contributing nothing, not crashing."""
    monkeypatch.setenv("INGEST_MODE", "live")
    monkeypatch.delenv("AWS_EVENT_ACCOUNTS", raising=False)
    assert build_ingestor(_ORG, env={}) is None


# ═════════════════════════════════════════════════════════════════════════════
# G2 — per-org config resolution reads back the Owner-pinned accounts
# ═════════════════════════════════════════════════════════════════════════════

def _connected_record(**over):
    record = {
        "status": "connected",
        "partition": PARTITION_AWS,
        "scopes": [
            {"scope_id": _ACCOUNT, "role_arn": f"arn:aws:iam::{_ACCOUNT}:role/RO",
             "external_id": "ext-abc", "regions": [_REGION]},
            {"scope_id": "444455556666", "role_arn": "arn:aws:iam::444455556666:role/RO",
             "regions": ["us-west-2"]},
        ],
    }
    record.update(over)
    return record


def test_g2_pinned_accounts_are_read_back_from_the_connector_record():
    config = config_from_connector_record(_connected_record())
    assert config is not None
    assert config.account_ids == [_ACCOUNT, "444455556666"]


def test_g2_unconnected_or_unpinned_record_contributes_nothing():
    assert config_from_connector_record(None) is None
    assert config_from_connector_record(_connected_record(status="not_configured")) is None
    assert config_from_connector_record(_connected_record(scopes=[])) is None


def test_g2_env_override_wins_over_the_connector_record(monkeypatch):
    """An explicit operator config always beats the Integration Hub record."""
    monkeypatch.setenv("INGEST_MODE", "live")
    env = {"AWS_EVENT_ACCOUNTS": json.dumps([{"account_id": "999988887777"}])}
    config = resolve_aws_event_config(
        _ORG, env=env, record_loader=lambda o: _connected_record()
    )
    assert config.account_ids == ["999988887777"]


def test_g2_falls_back_to_the_connector_record_when_no_env_config(monkeypatch):
    monkeypatch.setenv("INGEST_MODE", "live")
    config = resolve_aws_event_config(
        _ORG, env={}, record_loader=lambda o: _connected_record()
    )
    assert config.account_ids == [_ACCOUNT, "444455556666"]
    assert config.metadata["source"] == "integration_hub"


def test_g2_inline_secret_in_config_is_rejected(monkeypatch):
    """The no-secret-in-config rule holds through the new resolution path."""
    monkeypatch.setenv("INGEST_MODE", "live")
    env = {"AWS_EVENT_ACCOUNTS": json.dumps(
        [{"account_id": _ACCOUNT, "aws_secret_access_key": "AKIAsecret"}]
    )}
    with pytest.raises(AWSEventConfigError):
        resolve_aws_event_config(_ORG, env=env, record_loader=lambda o: None)


# ═════════════════════════════════════════════════════════════════════════════
# G3 — the ExternalId survives the pin flow and reaches AssumeRole
# ═════════════════════════════════════════════════════════════════════════════

def test_g3_external_id_survives_config_resolution():
    """It was captured at pin time, used for the probe, then dropped on the floor."""
    config = config_from_connector_record(_connected_record())
    by_id = {a.account_id: a for a in config.accounts}
    assert by_id[_ACCOUNT].external_id == "ext-abc"
    assert by_id["444455556666"].external_id is None    # genuinely not set


def test_g3_external_id_is_passed_to_assume_role():
    """End-to-end: a trust policy that requires an ExternalId must be satisfiable."""
    seen = {}

    class _STS:
        def assume_role(self, **kwargs):
            seen.update(kwargs)
            return {"Credentials": {"AccessKeyId": "ASIA1", "SecretAccessKey": "s",
                                    "SessionToken": "t"}}

    class _F(AWSClientFactory):
        def client(self, service, *, region, credentials, partition=None):
            assert service == "sts"
            return _STS()

    account = config_from_connector_record(_connected_record()).accounts[0]
    auth = AWSAuthenticator(
        client_factory=_F(),
        hub_resolver=lambda o: AWSCredentials("AKIAHUB", "s", source="hub"),
        account_key_resolver=lambda o, a: None,
    )
    creds = auth.credentials_for(_ORG, account)

    assert creds.source == "assumed_role"
    assert seen.get("ExternalId") == "ext-abc"


# ═════════════════════════════════════════════════════════════════════════════
# G4 — a backlog larger than one poll is drained, not silently truncated
# ═════════════════════════════════════════════════════════════════════════════

def test_g4_cloudtrail_backlog_larger_than_the_page_budget_is_not_lost(monkeypatch):
    """CloudTrail is newest-first, so truncation used to strand the OLDER remainder.

    The reader kept the newest page and advanced the watermark past everything, so
    every older unread event became permanently unreachable. The descending
    backfill must now walk the whole backlog instead.
    """
    monkeypatch.setattr(aws_poll_source, "MAX_PAGES_PER_POLL", 1)
    records_in = [_mgmt_event(f"ct-{n}", _iso(_BASE + timedelta(minutes=n)))
                  for n in range(1, 9)]                 # 8 events, 2 per poll
    client = _OrderedCloudTrail(records_in)
    source = _source(_Factory(cloudtrail=client), surfaces=(SURFACE_CLOUDTRAIL,), page_size=2)

    records, checkpoint = _drain(source)

    assert sorted(r["provider_event_id"] for r in records) == sorted(
        e["eventID"] for e in records_in
    ), "older events were stranded by page-budget truncation"
    # The window drained, so the watermark is the newest event and no backfill
    # state is left behind.
    position = decode_position(json.loads(checkpoint)["scopes"][
        f"aws:{_ACCOUNT}:{_REGION}:{SURFACE_CLOUDTRAIL}"
    ])
    assert position.watermark == _iso(_BASE + timedelta(minutes=8))
    assert not position.backfilling


def test_g4_cloudwatch_reads_oldest_first_so_truncation_is_safe(monkeypatch):
    """Ascending reads make a truncated page a complete PREFIX of the backlog."""
    monkeypatch.setattr(aws_poll_source, "MAX_PAGES_PER_POLL", 1)
    items = [_alarm_item(_iso(_BASE + timedelta(minutes=n))) for n in range(1, 9)]
    client = _OrderedCloudWatch(items)
    source = _source(_Factory(cloudwatch=client), surfaces=(SURFACE_CLOUDWATCH,), page_size=2)

    records, checkpoint = _drain(source)

    assert len(records) == 8, "ascending truncation still lost events"
    assert all(c.get("ScanBy") == "TimestampAscending" for c in client.calls)
    assert watermark_of(json.loads(checkpoint)["scopes"][
        f"aws:{_ACCOUNT}:{_REGION}:{SURFACE_CLOUDWATCH}"
    ]) == _iso(_BASE + timedelta(minutes=8))


def test_g4_backfill_resumes_across_runs_without_re_reading(monkeypatch):
    """A backlog spanning runs continues where it stopped and never re-reads."""
    monkeypatch.setattr(aws_poll_source, "MAX_PAGES_PER_POLL", 1)
    records_in = [_mgmt_event(f"ct-{n}", _iso(_BASE + timedelta(minutes=n)))
                  for n in range(1, 7)]
    client = _OrderedCloudTrail(records_in)
    source = _source(_Factory(cloudtrail=client), surfaces=(SURFACE_CLOUDTRAIL,), page_size=2)

    first, checkpoint = _drain(source)
    second, _ = _drain(source, since=Checkpoint.create("aws_events", _ORG, checkpoint))

    assert len(first) == 6
    assert second == [], "a drained backlog was re-read on the next run"


# ═════════════════════════════════════════════════════════════════════════════
# G8 — instants, not strings; same-instant stragglers are not dropped
# ═════════════════════════════════════════════════════════════════════════════

def test_g8_timestamps_compare_as_instants_not_strings():
    """'…T03:00:00Z' and '…T03:00:00+00:00' are the same instant."""
    position = TimePosition(watermark="2026-07-14T03:00:00Z", boundary_ids=("a",))
    assert position.is_new("2026-07-14T03:00:00+00:00", "a") is False   # same instant
    assert position.is_new("2026-07-14T03:00:00.500000Z", "b") is True  # genuinely later
    assert position.is_new("2026-07-14T02:59:59Z", "c") is False


def test_g8_same_instant_straggler_is_ingested_not_dropped(monkeypatch):
    """CloudTrail is eventually consistent, so a second event in an already-recorded
    second is routine. The old ``ts <= watermark`` filter dropped it forever."""
    monkeypatch.setattr(aws_poll_source, "MAX_PAGES_PER_POLL", 50)
    same_instant = _iso(_BASE)
    records_in = [_mgmt_event("ct-1", same_instant), _mgmt_event("ct-2", same_instant)]
    client = _OrderedCloudTrail(records_in)
    source = _source(_Factory(cloudtrail=client), surfaces=(SURFACE_CLOUDTRAIL,))

    first, checkpoint = _drain(source)
    assert {r["provider_event_id"] for r in first} == {"ct-1", "ct-2"}

    # A third event lands LATE carrying the identical timestamp.
    records_in.append(_mgmt_event("ct-3", same_instant))
    second, _ = _drain(source, since=Checkpoint.create("aws_events", _ORG, checkpoint))

    assert [r["provider_event_id"] for r in second] == ["ct-3"], (
        "a same-instant straggler was dropped (or already-seen events were re-read)"
    )


def test_g8_position_round_trips_and_stays_readable_when_simple():
    """The opaque position keeps its plain-ISO form when there is nothing to carry."""
    assert encode_position(TimePosition(watermark="2026-07-14T03:00:00Z")) == \
        "2026-07-14T03:00:00Z"
    rich = TimePosition(watermark="2026-07-14T03:00:00Z", boundary_ids=("a", "b"),
                        ceiling="2026-07-14T02:00:00Z", pending_high="2026-07-14T05:00:00Z")
    assert decode_position(encode_position(rich)) == rich
    # A legacy plain-ISO checkpoint still decodes (no forced re-read on upgrade).
    assert watermark_of("2026-07-14T03:00:00Z") == "2026-07-14T03:00:00Z"
    assert decode_position("").watermark == ""


# ═════════════════════════════════════════════════════════════════════════════
# G5 — the EventBridge surface stays in V1 scope, with its boundary declared
# ═════════════════════════════════════════════════════════════════════════════

def test_g5_eventbridge_remains_a_default_v1_surface():
    """MSP-B1 names three V1 event classes and AC1 requires all three per account.

    The rule set is what ``events:Describe*/List*`` can honestly observe, so the
    surface stays in scope — narrowing it would silently drop a specified class.
    """
    assert SURFACE_EVENTBRIDGE in AWS_SURFACES
    assert SURFACE_EVENTBRIDGE in DEFAULT_POLL_SURFACES
    assert set(DEFAULT_POLL_SURFACES) == set(AWS_SURFACES)


def test_g5_eventbridge_boundary_is_documented():
    """The limitation is stated in the module, not left for a live run to discover."""
    note = aws_poll_source.EVENTBRIDGE_SURFACE_NOTE
    assert "rule" in note.lower()
    assert "no past-event read api" in note.lower()


# ═════════════════════════════════════════════════════════════════════════════
# G6 — per-account health reaches run health
# ═════════════════════════════════════════════════════════════════════════════

def test_g6_account_health_is_surfaced_into_run_health(monkeypatch):
    """AC8's report existed on the connector but never reached the run record."""
    from discovery import runner

    written = {}
    monkeypatch.setattr(runner, "_update_pinned_scope_health", lambda *a, **k: None)
    import app.db as db

    monkeypatch.setattr(db, "run_kv_get", lambda *a, **k: {})
    monkeypatch.setattr(db, "run_kv_set", lambda key, run_id, value: written.update(value))

    report = {
        "connector": "aws_events", "all_healthy": False,
        "failed_accounts": ["222233334444"],
        "accounts": [
            {"account_id": _ACCOUNT, "status": "ok", "message": "",
             "surfaces_ok": ["cloudtrail"], "surfaces_failed": {}, "throttle_events": 0},
            {"account_id": "222233334444", "status": "auth_failed",
             "message": "role revoked", "surfaces_ok": [], "surfaces_failed": {},
             "throttle_events": 0},
        ],
    }
    runner._surface_cloud_account_health(_ORG, "run-1", "aws_events", report)

    assert "AWS Events (222233334444)" in written
    failed = written["AWS Events (222233334444)"]
    assert failed["status"] == "auth_failed"
    assert failed["message"] == "role revoked"
    assert written["AWS Events (111122223333)"]["status"] == "ok"


def test_g6_health_surfacing_never_breaks_a_run(monkeypatch):
    """Health is advisory: a failure to record it must not fail the run."""
    from discovery import runner
    import app.db as db

    monkeypatch.setattr(runner, "_update_pinned_scope_health", lambda *a, **k: None)
    monkeypatch.setattr(db, "run_kv_get", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down")))
    runner._surface_cloud_account_health(
        _ORG, "run-1", "aws_events", {"accounts": [{"account_id": _ACCOUNT}]}
    )  # must not raise


# ═════════════════════════════════════════════════════════════════════════════
# G7 — GovCloud endpoints come from the CONFIGURED partition
# ═════════════════════════════════════════════════════════════════════════════

def test_g7_govcloud_without_a_region_no_longer_resolves_commercial_sts():
    """The load-bearing case: partition set, region absent.

    Deriving the partition from an absent region yielded the COMMERCIAL global STS
    endpoint for a GovCloud connection — a cross-partition call that can only fail,
    and one that reads like an auth problem rather than a config one.
    """
    assert resolve_service_endpoint_or_none("sts", None) == "https://sts.amazonaws.com"
    assert resolve_service_endpoint_or_none("sts", None, PARTITION_GOVCLOUD) != \
        "https://sts.amazonaws.com"
    assert resolve_service_endpoint_or_none("cloudwatch", "us-gov-west-1", PARTITION_GOVCLOUD) == \
        "https://monitoring.us-gov-west-1.amazonaws.com"


def test_g7_poll_source_passes_the_configured_partition_to_the_client():
    account = AWSAccountConfig(
        account_id=_ACCOUNT, regions=("us-gov-west-1",), partition=PARTITION_GOVCLOUD
    )
    factory = _Factory(cloudtrail=_OrderedCloudTrail([]))
    source = _source(factory, surfaces=(SURFACE_CLOUDTRAIL,), account=account)
    _drain(source)

    assert factory.partitions_seen
    assert all(p == PARTITION_GOVCLOUD for _s, _r, p in factory.partitions_seen)


def test_g7_client_factory_defaults_a_govcloud_region(monkeypatch):
    """GovCloud has no global STS and no implicit default region."""
    captured = {}

    class _Boto3:
        @staticmethod
        def client(service, **kwargs):
            captured.update(service=service, **kwargs)
            return object()

    import builtins

    real_import = builtins.__import__

    def _fake_import(name, *args, **kwargs):
        return _Boto3 if name == "boto3" else real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fake_import)
    Boto3ClientFactory().client(
        "sts", region=None, credentials=AWSCredentials("a", "b"),
        partition=PARTITION_GOVCLOUD,
    )
    assert captured["endpoint_url"] == "https://sts.us-gov-west-1.amazonaws.com"
    assert captured["region_name"] == "us-gov-west-1"


# ═════════════════════════════════════════════════════════════════════════════
# G10 — the hub-credential env fallback is off in production
# ═════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def _hub_env(monkeypatch):
    monkeypatch.setenv("AWS_EVENTS_HUB_ACCESS_KEY_ID", "AKIAFAKEFAKEFAKE")
    monkeypatch.setenv("AWS_EVENTS_HUB_SECRET_ACCESS_KEY", "fake-secret")
    monkeypatch.setattr(aws_auth, "_vault_static_credential", lambda o, c: None)
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    monkeypatch.delenv("REQUIRE_CONNECTOR_SECRETS", raising=False)


def test_g10_env_fallback_still_works_for_cli_and_standalone(_hub_env):
    assert aws_auth.default_hub_resolver(_ORG) is not None


@pytest.mark.parametrize(
    "name,value", [("ENVIRONMENT", "production"), ("REQUIRE_CONNECTOR_SECRETS", "1")]
)
def test_g10_env_fallback_is_refused_in_production(_hub_env, monkeypatch, name, value):
    """In production the hub key lives ONLY in the vault, per the credential story."""
    monkeypatch.setenv(name, value)
    assert aws_auth.default_hub_resolver(_ORG) is None
