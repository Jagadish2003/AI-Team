"""MSP-B1 / AT-641 (T1) — the shared cloud-connector skeleton contract suite.

Proves the two acceptance criteria and the skeleton mechanics that back them:

  * **AC4 (transport equivalence)** — B0's golden fixtures run through this native
    connector yield detector-visible events IDENTICAL to the B8 bridge path except
    ``source_system`` (``'aws'`` vs ``'bridge:aws'``). Verified for the AWS
    connector AND, through the same skeleton with ``provider='azure'``, for the
    Azure golden fixtures — the no-fork proof AT-641 exists to give MSP-B2.
  * **AC5 (admission)** — events enter through B7 admission: a seeded re-firing
    alarm arrives deduplicated into one active signal with an occurrence count,
    live through the native poll path, and the aggregate still opens back to its
    raw instances.

Plus the poll loop, per-scope opaque checkpoints, resumable first load,
mapper-invocation normalisation, loud-skip robustness, and org-scoping.

Pure-Python (in-memory poll source, raw store, and staging sink), so it runs
alongside the other MSP signal tests without the contract DB.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from database.models.ops_event_staging import OpsEventStagingRow
from discovery.ingest.aws_event_connector import (
    AWSEventConnector,
    SURFACE_CLOUDTRAIL,
    SURFACE_CLOUDWATCH,
    SURFACE_EVENTBRIDGE,
    aws_scope,
    aws_scopes,
    build_offline_aws_source,
)
from discovery.ingest.base import Checkpoint
from discovery.ingest.cloud_event_connector import (
    CloudEventConnector,
    CloudScope,
    PollPage,
    StaticCloudPollSource,
    _decode_positions,
    _encode_positions,
)
from discovery.ingest.ops_event_bridge import OpsEventBridgeIngestor
from discovery.ingest.ops_event_equivalence import MAPPER_TO_STAGING, load_golden_cases
from discovery.ingest.ops_event_staging_store import InMemoryStagingSink
from discovery.signals.evidence_store import InMemoryRawEventStore, resolve_raw_event

_ORG = "acme"
_DAY = datetime(2026, 7, 14, 0, 0, 0, tzinfo=timezone.utc)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _drain(connector, org_id=_ORG, since=None):
    """Drive a connector's ingest_changes fully; return (records, batches)."""
    batches = list(connector.ingest_changes(org_id, since))
    records = [r for b in batches for r in b.records]
    return records, batches


def _alarm_firing(n: int) -> dict:
    """One raw firing of a stuck HighCPU alarm (native state-change event shape)."""
    return {
        "version": "0",
        "id": f"cw-{n}",
        "detail-type": "CloudWatch Alarm State Change",
        "source": "aws.cloudwatch",
        "account": "111122223333",
        "time": (_DAY + timedelta(minutes=5 * n)).isoformat().replace("+00:00", "Z"),
        "region": "us-east-1",
        "resources": ["arn:aws:cloudwatch:us-east-1:111122223333:alarm:HighCPU"],
        "detail": {
            "alarmName": "HighCPU",
            "state": {"value": "ALARM", "reason": "Threshold Crossed"},
            "previousState": {"value": "ALARM"},
        },
    }


def _aws_connector(scope_events, *, raw_store=None, page_size=500, budget=None):
    source = StaticCloudPollSource(scope_events, page_size=page_size)
    return AWSEventConnector(source, raw_store=raw_store, budget=budget)


_AWS_SURFACE_OF = {
    "map_cloudwatch": SURFACE_CLOUDWATCH,
    "map_eventbridge": SURFACE_EVENTBRIDGE,
    "map_cloudtrail": SURFACE_CLOUDTRAIL,
}
_AZURE_SURFACE_OF = {
    "map_azure_monitor": "azure_monitor",
    "map_azure_activity_log": "azure_activity",
}


def _native_events(cases, provider, surface_of, raw_store):
    """Run golden cases through the skeleton; return {signal_id: event_dict}, connector."""
    scope_events = []
    for case in cases:
        scope = CloudScope(
            provider=provider, account="acct",
            surface=surface_of[case["mapper"]], mapper=case["mapper"],
        )
        scope_events.append((scope, [case["raw"]]))
    source = StaticCloudPollSource(scope_events)
    connector = CloudEventConnector(
        source, provider=provider, connector_id=f"{provider}_events", raw_store=raw_store,
    )
    records, _ = _drain(connector)
    return {r["event"]["signal_id"]: r["event"] for r in records}, connector


def _bridge_events(cases, raw_store):
    """Run the same golden cases through the B8 bridge; return {signal_id: event_dict}."""
    sink = InMemoryStagingSink()
    rows = []
    for case in cases:
        provider, source_format = MAPPER_TO_STAGING[case["mapper"]]
        rows.append(OpsEventStagingRow(
            org_id=_ORG, provider=provider, source_format=source_format,
            batch_id=f"golden:{provider}", provider_event_id=f"golden:{case['name']}",
            raw=case["raw"],
        ))
    sink.insert_rows(rows)
    ingestor = OpsEventBridgeIngestor(sink, raw_store=raw_store, batch_size=1000)
    out = {}
    for batch in ingestor.ingest_changes(_ORG, None):
        for rec in batch.records:
            out[rec["event"]["signal_id"]] = rec["event"]
    return out


_GOLDEN = load_golden_cases()
_GOLDEN_AWS = [c for c in _GOLDEN if "azure" not in c["mapper"]]
_GOLDEN_AZURE = [c for c in _GOLDEN if "azure" in c["mapper"]]


# ─────────────────────────────────────────────────────────────────────────────
# Poll loop + mapper invocation
# ─────────────────────────────────────────────────────────────────────────────

def test_polls_all_scopes_and_emits_normalised_events():
    connector = _aws_connector([
        (aws_scope("111122223333", SURFACE_CLOUDWATCH, region="us-east-1"), [_alarm_firing(1)]),
        (aws_scope("111122223333", SURFACE_CLOUDTRAIL, region="us-east-1"), [{
            "eventID": "ct-1", "eventTime": "2026-07-14T03:00:00Z",
            "eventSource": "sts.amazonaws.com", "eventName": "AssumeRole",
            "userIdentity": {"arn": "arn:aws:iam::111122223333:user/alice"},
            "resources": [{"ARN": "arn:aws:iam::111122223333:role/admin"}],
        }]),
    ])
    records, _ = _drain(connector)
    assert len(records) == 2
    by_id = {r["provider_event_id"]: r for r in records}
    # Mapper invocation produced the normalised, provider-agnostic shape.
    cw = by_id["cw-1"]["event"]
    assert cw["source_system"] == "aws"          # re-stamped to the provider family
    assert cw["event_class"] == "state_change"
    assert cw["resource_type"] == "monitoring"
    assert cw["severity"] == "high"
    ct = by_id["ct-1"]["event"]
    assert ct["event_class"] == "access"
    assert ct["resource_type"] == "identity"
    # The connector adds no detector-visible fields of its own — records carry the
    # change vocabulary + trace-back only.
    assert by_id["cw-1"]["change_kind"] == "created"
    assert by_id["cw-1"]["surface"] == SURFACE_CLOUDWATCH
    assert by_id["cw-1"]["account"] == "111122223333"


def test_reports_deletes_is_false():
    # Append-only observation stream — the limitation is declared, not faked.
    assert AWSEventConnector.reports_deletes is False


def test_org_id_is_required():
    connector = _aws_connector([])
    with pytest.raises(ValueError):
        list(connector.ingest_changes("", None))


def test_unknown_mapper_scope_is_loud_skipped_not_fatal():
    bad = CloudScope(provider="aws", account="a", surface="mystery", mapper="map_nonexistent")
    good = aws_scope("a", SURFACE_CLOUDWATCH)
    source = StaticCloudPollSource([(bad, [{"id": "x"}]), (good, [_alarm_firing(1)])])
    connector = AWSEventConnector(source)
    records, _ = _drain(connector)
    # The bad scope contributed nothing; the good one still produced its event.
    assert [r["provider_event_id"] for r in records] == ["cw-1"]


def test_mapper_exception_is_loud_skipped_not_fatal(monkeypatch):
    import discovery.ingest.cloud_event_connector as mod

    def boom(payload, *, org_id):
        raise RuntimeError("provider payload exploded")

    monkeypatch.setitem(mod.MAPPERS, "map_cloudwatch", boom)
    connector = _aws_connector([(aws_scope("a", SURFACE_CLOUDWATCH), [_alarm_firing(1), _alarm_firing(2)])])
    records, batches = _drain(connector)
    # No records, but the run completed with one terminal batch (not a crash).
    assert records == []
    assert batches and batches[-1].is_complete


def test_provider_mismatch_scope_is_skipped():
    # A shared poll source that returns an off-provider scope: the aws connector
    # ignores it rather than mis-mapping it.
    azure_scope = CloudScope(provider="azure", account="s1", surface="azure_monitor", mapper="map_azure_monitor")
    aws_scope_ = aws_scope("a", SURFACE_CLOUDWATCH)
    source = StaticCloudPollSource([(azure_scope, [{"data": {}}]), (aws_scope_, [_alarm_firing(1)])])
    connector = AWSEventConnector(source)
    records, _ = _drain(connector)
    assert [r["provider"] for r in records] == ["aws"]


# ─────────────────────────────────────────────────────────────────────────────
# Per-scope checkpoints + resumable first load + incremental
# ─────────────────────────────────────────────────────────────────────────────

def test_checkpoint_is_opaque_json_and_round_trips():
    positions = {"aws:a:*:cloudwatch": "3", "aws:a:*:cloudtrail": "7"}
    encoded = _encode_positions(positions)
    assert isinstance(encoded, str)
    assert _decode_positions(encoded) == positions
    # Deterministic (sorted) — two encodings of identical state are byte-identical.
    assert _encode_positions(positions) == _encode_positions(dict(reversed(list(positions.items()))))


def test_degenerate_checkpoint_degrades_to_full_repoll():
    assert _decode_positions(None) == {}
    assert _decode_positions("") == {}
    assert _decode_positions("not json") == {}
    assert _decode_positions('{"unexpected": 1}') == {}


def test_idle_poll_yields_single_empty_batch_echoing_positions():
    connector = _aws_connector([(aws_scope("a", SURFACE_CLOUDWATCH), [])])
    records, batches = _drain(connector)
    assert records == []
    assert len(batches) == 1
    assert batches[0].is_complete and batches[0].is_empty


def test_first_load_streams_resumably_with_one_terminal_batch():
    # Small page size forces multiple batches; every batch carries a valid opaque
    # checkpoint and exactly one is flagged is_complete=True.
    firings = [_alarm_firing(n) for n in range(1, 6)]
    connector = _aws_connector([(aws_scope("a", SURFACE_CLOUDWATCH), firings)], page_size=2)
    records, batches = _drain(connector)
    assert len(records) == 5
    assert sum(1 for b in batches if b.is_complete) == 1
    assert batches[-1].is_complete
    for b in batches:
        assert _decode_positions(b.next_checkpoint)  # every batch is a resume point


def test_incremental_run_only_returns_new_events():
    scope = aws_scope("a", SURFACE_CLOUDWATCH)
    first_source = StaticCloudPollSource([(scope, [_alarm_firing(1), _alarm_firing(2)])])
    c1 = AWSEventConnector(first_source)
    records1, batches1 = _drain(c1)
    assert len(records1) == 2
    final = batches1[-1].next_checkpoint

    # Second run: same two events already seen, plus one new firing.
    second_source = StaticCloudPollSource(
        [(scope, [_alarm_firing(1), _alarm_firing(2), _alarm_firing(3)])]
    )
    c2 = AWSEventConnector(second_source)
    checkpoint = Checkpoint.create(c2.connector_id, _ORG, final)
    records2, _ = _drain(c2, since=checkpoint)
    assert [r["provider_event_id"] for r in records2] == ["cw-3"]


# ─────────────────────────────────────────────────────────────────────────────
# AC5 — admission hand-off (dedup with a count, live through the native path)
# ─────────────────────────────────────────────────────────────────────────────

def test_ac5_refiring_alarm_folds_to_one_active_signal_with_count():
    store = InMemoryRawEventStore()
    firings = [_alarm_firing(n) for n in range(1, 6)]  # 5 distinct firings, same alarm
    connector = _aws_connector([(aws_scope("a", SURFACE_CLOUDWATCH), firings)], raw_store=store)
    records, _ = _drain(connector)

    signals = connector.active_signals(_ORG)
    assert len(signals) == 1                      # the 5 re-fires deduplicated to ONE
    sig = signals[0]
    assert sig.occurrence_count == 5              # ...carrying the count
    assert sig.is_recurrence
    assert sig.first_seen < sig.last_seen         # correct first/last span
    # The aggregate opens back to its real instances (aggregation compresses
    # volume, never evidence).
    raws = sig.resolve_raw_instances(store)
    assert len(raws) == 5
    assert {r["id"] for r in raws} == {f"cw-{n}" for n in range(1, 6)}


def test_ac5_exact_redelivery_is_idempotent():
    # The same firing delivered twice (identical provider event id) must not
    # double-count and must not be re-emitted as a second record.
    connector = _aws_connector([(aws_scope("a", SURFACE_CLOUDWATCH), [_alarm_firing(1), _alarm_firing(1)])])
    records, _ = _drain(connector)
    assert len(records) == 1
    [sig] = connector.active_signals(_ORG)
    assert sig.occurrence_count == 1


def test_ac5_budget_defers_loudly_never_silently():
    firings = [_alarm_firing(n) for n in range(1, 6)]  # 5 firings, budget of 3
    connector = _aws_connector([(aws_scope("a", SURFACE_CLOUDWATCH), firings)], budget=3)
    records, _ = _drain(connector)
    report = connector.budget_report()
    assert report.breached
    assert report.processed == 3
    assert report.deferred == 2
    # Only the processed window folded into the active signal.
    [sig] = connector.active_signals(_ORG)
    assert sig.occurrence_count == 3


def test_offline_aws_source_demonstrates_dedup():
    # The shipped offline fixture seeds a re-firing HighCPU alarm — a run with no
    # AWS account still shows admission folding it.
    store = InMemoryRawEventStore()
    connector = AWSEventConnector(build_offline_aws_source(), raw_store=store)
    records, _ = _drain(connector)
    signals = connector.active_signals(_ORG)
    high_cpu = [s for s in signals if s.resource_id.endswith("alarm:HighCPU")]
    assert len(high_cpu) == 1
    assert high_cpu[0].occurrence_count == 3
    # EventBridge (1) + CloudTrail (2) events are distinct signals, not folded.
    assert len(signals) == 4


def test_aws_scopes_builds_all_surfaces_per_account():
    scopes = aws_scopes(["111", "222"], regions=["us-east-1"])
    assert len(scopes) == 6                        # 2 accounts × 1 region × 3 surfaces
    assert {s.surface for s in scopes} == {SURFACE_CLOUDWATCH, SURFACE_EVENTBRIDGE, SURFACE_CLOUDTRAIL}
    assert {s.scope_key for s in scopes}.__len__() == 6  # all distinct


# ─────────────────────────────────────────────────────────────────────────────
# AC4 — transport equivalence with the B8 bridge
# ─────────────────────────────────────────────────────────────────────────────

def _assert_equivalent(cases, provider, surface_of):
    native_store = InMemoryRawEventStore()
    bridge_store = InMemoryRawEventStore()
    native, _connector = _native_events(cases, provider, surface_of, native_store)
    bridge = _bridge_events(cases, bridge_store)

    assert set(native) == set(bridge) == {c["expected"]["signal_id"] for c in cases}
    for signal_id, native_event in native.items():
        bridge_event = bridge[signal_id]
        # Every detector-visible field is identical EXCEPT source_system; provenance
        # is transport-specific and verified via stable resolution, not compared.
        for field in sorted(set(native_event) | set(bridge_event)):
            if field in ("source_system", "provenance"):
                continue
            assert native_event[field] == bridge_event[field], (
                f"{signal_id}: field {field!r} diverged — "
                f"native={native_event[field]!r} bridge={bridge_event[field]!r}"
            )
        # The one intentional difference: 'aws' vs 'bridge:aws'.
        assert native_event["source_system"] == provider
        assert bridge_event["source_system"] == f"bridge:{provider}"
        # And the recurrence identity is preserved across both transports.
        assert native_event["event_signature"] == bridge_event["event_signature"]


def test_ac4_native_aws_equivalent_to_bridge_except_source_system():
    _assert_equivalent(_GOLDEN_AWS, "aws", _AWS_SURFACE_OF)


def test_ac4_native_azure_equivalent_via_the_same_skeleton():
    # The no-fork proof for MSP-B2: the SAME skeleton with provider='azure' makes
    # the Azure golden fixtures equivalent to their bridged twins.
    _assert_equivalent(_GOLDEN_AZURE, "azure", _AZURE_SURFACE_OF)


def test_ac4_native_evidence_resolves_to_the_raw_provider_payload():
    store = InMemoryRawEventStore()
    _native, connector = _native_events(_GOLDEN_AWS, "aws", _AWS_SURFACE_OF, store)
    # Re-map one golden case as the connector did and confirm its evidence pointer
    # resolves back to the identical raw payload it was built from.
    case = next(c for c in _GOLDEN_AWS if c["mapper"] == "map_cloudtrail" and not c["raw"].get("errorCode"))
    from discovery.signals.reference_mappers import map_cloudtrail
    event = map_cloudtrail(case["raw"], org_id=_ORG)
    event.source_system = "aws"
    from app.provenance import EvidencePointer
    event.provenance = EvidencePointer.observed(
        source_system="aws", source_artifact=event.signal_id,
        source_timestamp=event.observed_at, source_artifact_type="cloud_event",
    ).to_dict()
    assert resolve_raw_event(store, _ORG, event) == case["raw"]
