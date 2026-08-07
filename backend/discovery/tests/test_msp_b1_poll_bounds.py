"""MSP-B1 — the poll phase is BOUNDED per run (the "stuck at 15%" regression).

A discovery run with a ``cloud_ops`` pack drives the native AWS connector. Against a
real account the connector hung the whole run: the shared skeleton's per-scope poll
loop was ``while True: ... if not page.has_more: break``, so one run kept paging
until the provider's ENTIRE backlog drained. For CloudTrail that is the full 90-day
``LookupEvents`` retention of every managed account × region, read through an API
rate-limited to a couple of calls a second — hours of paging inside a single
ingestion stage, with the run's progress frozen. Three things made it worse:

  * the MSP-B7 per-run event budget was enforced at ADMISSION only, so once it was
    exhausted the connector kept fetching pages whose every event it then deferred —
    the budget bounded the data but never the work (contradicting B7 T4's own design
    note: a budget must stop the run *processing* everything);
  * throttle retries restarted the whole multi-page reader, re-reading every page
    already fetched, so sustained throttling made the work quadratic;
  * every emitted event drove a per-record ``ingestion.artifact_changed`` telemetry
    write plus a retrieval mark-stale-and-enqueue transaction — for records that are
    not retrieval artifacts at all.

This suite pins each fix. The load-bearing invariant throughout: stopping early is
RESUME, not truncation — the advanced position is checkpointed, the stop is logged,
and :meth:`poll_report` names the scope and the bound that stopped it.

Pure-Python: in-memory poll sources and seeded fake clients. No AWS, no database.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import pytest

from discovery.ingest import aws_poll_source, change_runner
from discovery.ingest.aws_auth import (
    AWSAccountConfig,
    AWSAuthenticator,
    AWSClientFactory,
    AWSCredentials,
)
from discovery.ingest.aws_event_connector import (
    SURFACE_CLOUDTRAIL,
    SURFACE_CLOUDWATCH,
    AWSEventConnector,
    aws_scope,
)
from discovery.ingest.aws_poll_source import CLOUDTRAIL_MAX_RESULTS, AWSLivePollSource
from discovery.ingest.aws_watermark import decode_position
from discovery.ingest.azure_events import AzureEventIngestor
from discovery.ingest.base import Checkpoint
from discovery.ingest.cloud_event_connector import (
    STOP_BUDGET,
    STOP_DEADLINE,
    STOP_POLL_CAP,
    CloudPollSource,
    PollPage,
    StaticCloudPollSource,
    _decode_positions,
)
from discovery.ingest.ops_event_bridge import OpsEventBridgeIngestor

_ORG = "bounds-org"
_REGION = "us-east-1"
_ACCOUNT = "111122223333"
_DAY = datetime(2026, 7, 14, 0, 0, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _alarm_firing(n: int) -> Dict[str, Any]:
    """One raw firing of a stuck alarm (native Alarm State Change shape)."""
    return {
        "version": "0",
        "id": f"cw-{n}",
        "detail-type": "CloudWatch Alarm State Change",
        "source": "aws.cloudwatch",
        "account": _ACCOUNT,
        "time": _iso(_DAY + timedelta(minutes=n)),
        "region": _REGION,
        "resources": [f"arn:aws:cloudwatch:{_REGION}:{_ACCOUNT}:alarm:HighCPU"],
        "detail": {
            "alarmName": "HighCPU",
            "state": {"value": "ALARM", "reason": "Threshold Crossed"},
            "previousState": {"value": "OK"},
        },
    }


class _EndlessSource(CloudPollSource):
    """A scope whose backlog NEVER drains — the shape that hung the run.

    Every poll returns one fresh event and reports ``has_more=True`` for ever, with a
    monotonically advancing position (so the "position did not move" guard cannot be
    what stops it). Only a real per-run bound can end this loop.
    """

    def __init__(self, scopes: List[Any]) -> None:
        self._scopes = list(scopes)
        self.polls_by_scope: Dict[str, int] = {}

    def list_scopes(self, org_id: str) -> List[Any]:
        return list(self._scopes)

    def poll(self, org_id: str, scope: Any, position: str) -> PollPage:
        consumed = int(position or 0)
        self.polls_by_scope[scope.scope_key] = self.polls_by_scope.get(scope.scope_key, 0) + 1
        return PollPage(
            events=[_alarm_firing(consumed + 1)],
            next_position=str(consumed + 1),
            has_more=True,
        )

    @property
    def total_polls(self) -> int:
        return sum(self.polls_by_scope.values())


def _drain(connector, since: Optional[Checkpoint] = None):
    batches = list(connector.ingest_changes(_ORG, since))
    return [r for b in batches for r in b.records], batches


# ═════════════════════════════════════════════════════════════════════════════
# The poll loop terminates — three independent bounds, each loud
# ═════════════════════════════════════════════════════════════════════════════

def test_endless_backlog_stops_at_the_per_scope_poll_cap():
    scope = aws_scope(_ACCOUNT, SURFACE_CLOUDWATCH, region=_REGION)
    source = _EndlessSource([scope])
    connector = AWSEventConnector(source, max_polls_per_scope=3, poll_deadline_seconds=0)

    records, batches = _drain(connector)

    assert source.total_polls == 3, "the poll loop is still unbounded"
    assert len(records) == 3
    report = connector.poll_report()
    assert report["complete"] is False
    assert report["backlog_remaining"] == {scope.scope_key: STOP_POLL_CAP}
    # Stopping early is resume, not truncation: a terminal batch still carries the
    # advanced position so the checkpoint moves and the next run continues.
    assert sum(1 for b in batches if b.is_complete) == 1
    assert _decode_positions(batches[-1].next_checkpoint)[scope.scope_key] == "3"


def test_the_remainder_resumes_on_the_next_run_without_re_reading():
    scope = aws_scope(_ACCOUNT, SURFACE_CLOUDWATCH, region=_REGION)
    source = _EndlessSource([scope])

    first = AWSEventConnector(source, max_polls_per_scope=2, poll_deadline_seconds=0)
    records1, batches1 = _drain(first)
    checkpoint = Checkpoint.create(first.connector_id, _ORG, batches1[-1].next_checkpoint)

    second = AWSEventConnector(source, max_polls_per_scope=2, poll_deadline_seconds=0)
    records2, _ = _drain(second, since=checkpoint)

    ids1 = [r["provider_event_id"] for r in records1]
    ids2 = [r["provider_event_id"] for r in records2]
    assert ids1 == ["cw-1", "cw-2"]
    assert ids2 == ["cw-3", "cw-4"], "the run did not resume where the bound stopped it"
    assert not set(ids1) & set(ids2), "events were re-read across the bound"


def test_budget_exhaustion_stops_fetching_not_just_admission():
    """The B7 budget must end the poll, not just defer what the poll already paid for."""
    scope = aws_scope(_ACCOUNT, SURFACE_CLOUDWATCH, region=_REGION)
    source = _EndlessSource([scope])
    # Budget of 2 events, but a generous poll cap: only the budget can stop this.
    connector = AWSEventConnector(
        source, budget=2, max_polls_per_scope=500, poll_deadline_seconds=0
    )

    records, _ = _drain(connector)

    # Two polls fill the budget; the third is never made. Before the fix this scope
    # paged for ever while admission deferred every event it fetched.
    assert source.total_polls == 2
    assert len(records) == 2
    assert connector.poll_report()["backlog_remaining"] == {scope.scope_key: STOP_BUDGET}
    # Nothing was DEFERRED, because nothing was fetched only to be thrown away —
    # that is the point of stopping the fetch. (A page that straddles the budget
    # still defers-and-counts within itself, which BudgetReport reports as breached.)
    assert connector.budget_report().deferred == 0


def test_deadline_stops_continuations_but_never_a_scopes_first_poll(monkeypatch):
    """Volume is not time: a throttled provider needs a wall-clock bound too.

    Fairness matters as much as the bound — the deadline is consulted only for
    CONTINUATION polls, so a late scope is never starved by an earlier scope's
    backlog. Both scopes must still be polled once.
    """
    from discovery.ingest import cloud_event_connector as skeleton

    class _Clock:
        """Deterministic clock: every reading is one second later than the last.

        A real elapsed-time assertion is untestable here — ``time.monotonic()`` has
        ~15ms granularity on Windows, so dozens of in-memory polls fit inside one tick.
        """

        def __init__(self) -> None:
            self.now = 1000.0

        def monotonic(self) -> float:
            self.now += 1.0
            return self.now

    monkeypatch.setattr(skeleton, "time", _Clock())

    scopes = [
        aws_scope(_ACCOUNT, SURFACE_CLOUDWATCH, region=_REGION),
        aws_scope("444455556666", SURFACE_CLOUDWATCH, region=_REGION),
    ]
    source = _EndlessSource(scopes)
    connector = AWSEventConnector(
        source, max_polls_per_scope=0, poll_deadline_seconds=0.5
    )

    _drain(connector)

    assert source.polls_by_scope == {s.scope_key: 1 for s in scopes}
    assert connector.poll_report()["backlog_remaining"] == {
        s.scope_key: STOP_DEADLINE for s in scopes
    }


def test_a_drained_scope_reports_a_complete_poll():
    """The bounds must not make an ordinary, fully-drained poll look partial."""
    scope = aws_scope(_ACCOUNT, SURFACE_CLOUDWATCH, region=_REGION)
    source = StaticCloudPollSource([(scope, [_alarm_firing(1), _alarm_firing(2)])], page_size=1)
    connector = AWSEventConnector(source, max_polls_per_scope=4)

    records, _ = _drain(connector)

    assert len(records) == 2
    report = connector.poll_report()
    assert report["complete"] is True
    assert report["backlog_remaining"] == {}
    assert report["polls"] == 2 and report["scopes_polled"] == 1


def test_an_early_stop_degrades_the_runner_reported_status():
    """A partial ingest must never read as a clean one in run health (MSP-B1 posture)."""
    from discovery import runner

    assert runner._cloud_poll_health(object()) == {}   # never raises on a source with none

    class _Partial:
        def poll_report(self):
            return {"complete": False, "backlog_remaining": {"aws:1:*:cloudtrail": STOP_DEADLINE}}

    assert runner._cloud_poll_health(_Partial())["complete"] is False


# ═════════════════════════════════════════════════════════════════════════════
# Transport-only records must not drive per-event retrieval work
# ═════════════════════════════════════════════════════════════════════════════

def test_cloud_event_connectors_declare_they_produce_no_retrieval_content():
    for ingestor in (AWSEventConnector, AzureEventIngestor, OpsEventBridgeIngestor):
        assert ingestor.produces_retrieval_content is False, ingestor.__name__


def test_change_runner_skips_per_record_telemetry_and_freshness_for_them(monkeypatch):
    """A cloud event is an observation, not an indexed artifact.

    Per-record emission cost a telemetry row AND a mark-stale-and-enqueue transaction
    per event — and parked an unresolvable row in the retrieval refresh queue — for
    artifacts nothing chunks, resolves, or can ever delete.
    """
    calls = {"emit": 0}

    def _fail(*_a, **_k):
        calls["emit"] += 1

    monkeypatch.setattr(change_runner, "_emit_artifact_changed", _fail)

    scope = aws_scope(_ACCOUNT, SURFACE_CLOUDWATCH, region=_REGION)
    source = StaticCloudPollSource([(scope, [_alarm_firing(1), _alarm_firing(2)])])
    connector = AWSEventConnector(source)

    saved: List[Checkpoint] = []
    result = change_runner.ingest_with_checkpoint(
        connector,
        _ORG,
        read_checkpoint=lambda o, c: None,
        save_checkpoint=saved.append,
    )

    assert result.records == 2 and result.ok
    assert calls["emit"] == 0, "transport-only records still drove per-record retrieval work"
    assert saved, "the checkpoint must still advance"


def test_content_connectors_are_unaffected(monkeypatch):
    """The exemption is opt-in: a content connector still emits per record."""
    seen: List[int] = []
    monkeypatch.setattr(
        change_runner,
        "_emit_artifact_changed",
        lambda org, cid, records, **kw: seen.append(len(records)),
    )

    from discovery.ingest.base import DeltaBatch
    from discovery.ingest.base import ChangeBasedIngestor

    class _Docs(ChangeBasedIngestor):
        connector_id = "documents"

        def ingest_changes(self, org_id, since):
            yield DeltaBatch(
                records=[{"artifact_id": "a", "change_kind": "created"}],
                next_checkpoint="1",
                is_complete=True,
            )

    change_runner.ingest_with_checkpoint(
        _Docs(), _ORG, read_checkpoint=lambda o, c: None, save_checkpoint=lambda cp: None
    )
    assert seen == [1]


# ═════════════════════════════════════════════════════════════════════════════
# AWS reader-level fixes: page-size clamp, bounded first load, throttle progress
# ═════════════════════════════════════════════════════════════════════════════

class _RecordingCloudTrail:
    """LookupEvents: newest-first, honours StartTime/EndTime, records every call."""

    def __init__(self, records: List[Dict[str, Any]], *, throttle_on_call: int = 0) -> None:
        self.records = records
        self.calls: List[Dict[str, Any]] = []
        self._throttle_on_call = throttle_on_call
        self._call_number = 0

    def lookup_events(self, **kwargs):
        self._call_number += 1
        self.calls.append(dict(kwargs))
        if self._call_number == self._throttle_on_call:
            raise _Throttled()
        start, end = kwargs.get("StartTime"), kwargs.get("EndTime")
        rows = []
        for record in self.records:
            ts = datetime.fromisoformat(record["eventTime"].replace("Z", "+00:00"))
            if start is not None and ts < start:
                continue
            if end is not None and ts > end:
                continue
            rows.append(record)
        rows.sort(key=lambda r: r["eventTime"], reverse=True)
        offset = int(kwargs.get("NextToken") or 0)
        limit = kwargs.get("MaxResults") or len(rows)
        page = rows[offset:offset + limit]
        nxt = offset + len(page)
        return {
            "Events": [{"CloudTrailEvent": json.dumps(r)} for r in page],
            "NextToken": str(nxt) if nxt < len(rows) else None,
        }


class _Throttled(Exception):
    def __init__(self) -> None:
        super().__init__("Rate exceeded")
        self.response = {"Error": {"Code": "Throttling", "Message": "Rate exceeded"}}


class _Factory(AWSClientFactory):
    def __init__(self, cloudtrail) -> None:
        self.cloudtrail = cloudtrail

    def client(self, service, *, region, credentials, partition=None):
        assert service == "cloudtrail"
        return self.cloudtrail


def _mgmt_event(event_id: str, ts: str) -> Dict[str, Any]:
    return {
        "eventID": event_id, "eventTime": ts, "eventName": "AssumeRole",
        "eventSource": "sts.amazonaws.com", "eventCategory": "Management",
        "managementEvent": True,
        "userIdentity": {"type": "IAMUser", "arn": f"arn:aws:iam::{_ACCOUNT}:user/alice"},
    }


def _live_source(client, *, page_size=100, max_backfill_days=None, max_throttle_retries=5):
    auth = AWSAuthenticator(
        client_factory=_Factory(client),
        hub_resolver=lambda o: None,
        account_key_resolver=lambda o, a: AWSCredentials(f"AKIA{a}", "s", source="direct_keys"),
    )
    return AWSLivePollSource(
        [AWSAccountConfig(account_id=_ACCOUNT, regions=(_REGION,))],
        auth,
        surfaces=(SURFACE_CLOUDTRAIL,),
        page_size=page_size,
        max_backfill_days=max_backfill_days,
        max_throttle_retries=max_throttle_retries,
        sleeper=lambda _s: None,
    )


def test_cloudtrail_max_results_is_clamped_to_the_api_maximum():
    """LookupEvents documents MaxResults <= 50; asking for 100 is not a contract."""
    client = _RecordingCloudTrail([_mgmt_event("ct-1", _iso(_DAY))])
    source = _live_source(client, page_size=100)

    source.poll(_ORG, aws_scope(_ACCOUNT, SURFACE_CLOUDTRAIL, region=_REGION), "")

    assert client.calls, "CloudTrail was never called"
    assert all(c["MaxResults"] == CLOUDTRAIL_MAX_RESULTS for c in client.calls)


def test_initial_backfill_depth_is_bounded_and_says_so(monkeypatch, caplog):
    """A 90-day first load pinned the watermark for run after run.

    The descending walk now closes at a configured DEPTH below its own newest event,
    promotes the high-water mark, and resumes normal incremental polling — loudly.
    """
    monkeypatch.setattr(aws_poll_source, "MAX_PAGES_PER_POLL", 1)
    newest = _DAY
    # 90 days of history, one event per day, newest first.
    records = [_mgmt_event(f"ct-{d}", _iso(newest - timedelta(days=d))) for d in range(90)]
    client = _RecordingCloudTrail(records)
    source = _live_source(client, page_size=2, max_backfill_days=3)
    scope = aws_scope(_ACCOUNT, SURFACE_CLOUDTRAIL, region=_REGION)

    position = ""
    with caplog.at_level(logging.WARNING):
        for _ in range(10):
            page = source.poll(_ORG, scope, position)
            position = page.next_position
            if not page.has_more:
                break

    decoded = decode_position(position)
    assert not decoded.backfilling, "the initial backfill never closed"
    assert decoded.watermark == _iso(newest), "the high-water mark was not promoted"
    assert any("depth bound" in r.message for r in caplog.records), (
        "the bounded initial load was silent"
    )
    # And the bound is opt-out: 0 keeps the original unbounded walk.
    unbounded = _live_source(_RecordingCloudTrail(records), page_size=2, max_backfill_days=0)
    page = unbounded.poll(_ORG, scope, "")
    assert page.has_more and decode_position(page.next_position).backfilling


def test_throttle_retry_keeps_page_progress():
    """Retrying the READER re-read every page it had already fetched.

    At two calls a second on LookupEvents, throttling is routine — restarting the
    multi-page read on each one made the work quadratic and the poll look hung.
    """
    records = [_mgmt_event(f"ct-{n}", _iso(_DAY - timedelta(minutes=n))) for n in range(6)]
    # Throttle the SECOND API call — i.e. mid-pagination, after page 1 succeeded.
    client = _RecordingCloudTrail(records, throttle_on_call=2)
    source = _live_source(client, page_size=2)

    page = source.poll(_ORG, aws_scope(_ACCOUNT, SURFACE_CLOUDTRAIL, region=_REGION), "")

    # All six events still arrive (retried, never thinned)...
    assert len(page.events) == 6
    # ...and page 1 (no NextToken) was fetched exactly once — not re-read.
    first_page_calls = [c for c in client.calls if not c.get("NextToken")]
    assert len(first_page_calls) == 1, "the throttle retry re-read pages already fetched"
    assert source.health.to_dict()["accounts"][0]["throttle_events"] == 1
