"""MSP-B1 / AT-642 (T2) — the live AWS poll source.

The provider edge for the native AWS event connector: a
:class:`~discovery.ingest.cloud_event_connector.CloudPollSource` that turns the
managed-account config into scopes and, per scope, authenticates via
:class:`~discovery.ingest.aws_auth.AWSAuthenticator` (STS AssumeRole from the hub
identity, or direct per-account keys) and reads that surface's operational events
with EXACTLY the read-only API calls the partner IAM policy grants:

* **CloudWatch** — ``cloudwatch:DescribeAlarmHistory`` (alarm state changes),
  transformed into the ``CloudWatch Alarm State Change`` event shape the MSP-B0
  ``map_cloudwatch`` reference mapper consumes (the T1 skeleton then normalises it).
* **EventBridge** — ``events:ListRules`` + ``events:DescribeRule`` on the scoped
  rules (the *bounded* EventBridge surface reachable through the granted
  ``events:Describe*/List*`` — the event bus itself exposes no past-event read API;
  full event-stream ingestion via an archive replay is a documented follow-up).
* **CloudTrail** — ``cloudtrail:LookupEvents`` (management events).

Every scope carries its ``account_scope`` (the managed account id), which the
skeleton stamps onto every emitted record — so a multi-account run never loses
which account a signal belongs to (AT-642 AC1).

Across-run resume is by a per-scope **time watermark** (the newest event time seen
for that scope), carried as the skeleton's opaque per-scope checkpoint position.
Within a run each reader follows the provider's ``NextToken`` internally (bounded
by :data:`MAX_PAGES_PER_POLL`) and returns everything newer than the watermark in
one page; the raw provider-side volume is bounded downstream by the B7 admission
budget, not here.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Tuple

from .aws_auth import AWSAccountConfig, AWSAuthenticator, AWSCredentials
from .aws_event_connector import (
    AWS_SURFACE_MAPPERS,
    AWS_SURFACES,
    PROVIDER_AWS,
    SURFACE_CLOUDTRAIL,
    SURFACE_CLOUDWATCH,
    SURFACE_EVENTBRIDGE,
    aws_scope,
)
from .cloud_event_connector import CloudPollSource, CloudScope, PollPage

logger = logging.getLogger(__name__)

#: Safety cap on provider pages followed within a single poll() call, so a huge
#: backlog cannot spin unbounded. Hitting it is logged loudly (never silent).
MAX_PAGES_PER_POLL = 50

#: Default provider page size per surface API call.
DEFAULT_PAGE_SIZE = 100

#: Surface → the boto3 service name whose client reads it.
_SERVICE_FOR_SURFACE: Dict[str, str] = {
    SURFACE_CLOUDWATCH: "cloudwatch",
    SURFACE_EVENTBRIDGE: "events",
    SURFACE_CLOUDTRAIL: "cloudtrail",
}


# ─────────────────────────────────────────────────────────────────────────────
# Surface readers (pure over a client — tests inject a fake client)
# ─────────────────────────────────────────────────────────────────────────────

def _bounded_id(value: str, *, limit: int = 256) -> str:
    """Bound an identity string so a pathological value cannot bloat a key."""
    return value if len(value) <= limit else value[:limit]


def _alarm_history_to_state_change(
    item: Dict[str, Any], *, account_id: str, region: Optional[str]
) -> Optional[Dict[str, Any]]:
    """Adapt one ``DescribeAlarmHistory`` item to the Alarm State Change shape.

    The MSP-B0 ``map_cloudwatch`` reference mapper consumes the EventBridge-
    delivered *Alarm State Change* event (``detail.state.value`` / ``resources`` /
    ``detail-type``), not the ``DescribeAlarmHistory`` item shape. Reconciling the
    two here (the connector's provider edge) is exactly the gap the MSP-B8 T6
    equivalence harness flagged for B1 to resolve — so the native connector emits
    detector-identical events without changing the B0 mapper contract.

    Returns ``None`` (loud-skip) for an item with no alarm name / timestamp — it
    has no stable identity and cannot be a state-change event.
    """
    name = item.get("AlarmName")
    ts = item.get("Timestamp")
    if not name or not ts:
        return None
    item_type = item.get("HistoryItemType", "")
    new_state, old_state = _parse_alarm_history_states(item.get("HistoryData"))
    alarm_arn = f"arn:aws:cloudwatch:{region or ''}:{account_id}:alarm:{name}"
    return {
        "id": _bounded_id(f"{name}|{ts}|{item_type}"),
        "detail-type": "CloudWatch Alarm State Change",
        "source": "aws.cloudwatch",
        "account": account_id,
        "time": ts,
        "region": region,
        "resources": [alarm_arn],
        "detail": {
            "alarmName": name,
            "state": {"value": new_state, "reason": item.get("HistorySummary")},
            "previousState": {"value": old_state},
        },
    }


def _parse_alarm_history_states(history_data: Any) -> Tuple[str, str]:
    """Extract ``(newStateValue, oldStateValue)`` from a HistoryData blob (tolerant)."""
    data = history_data
    if isinstance(history_data, str):
        try:
            data = json.loads(history_data)
        except (TypeError, ValueError):
            data = {}
    if not isinstance(data, dict):
        return "", ""
    new_state = (data.get("newState") or {}).get("stateValue", "") if isinstance(data.get("newState"), dict) else ""
    old_state = (data.get("oldState") or {}).get("stateValue", "") if isinstance(data.get("oldState"), dict) else ""
    return new_state, old_state


def read_cloudwatch(
    client: Any, *, region: Optional[str], account_id: str, watermark: str, page_size: int
) -> Tuple[List[Dict[str, Any]], str]:
    """Read new CloudWatch alarm-history state changes since ``watermark``.

    Uses ``cloudwatch:DescribeAlarmHistory`` (StateUpdate items), following
    ``NextToken`` internally. Returns ``(events, new_watermark)`` where events are
    in the map_cloudwatch input shape and new_watermark is the newest item time.
    """
    events: List[Dict[str, Any]] = []
    newest = watermark
    token: Optional[str] = None
    for _ in range(MAX_PAGES_PER_POLL):
        params: Dict[str, Any] = {"HistoryItemType": "StateUpdate", "MaxRecords": page_size}
        if watermark:
            params["StartDate"] = watermark
        if token:
            params["NextToken"] = token
        resp = client.describe_alarm_history(**params) or {}
        for item in resp.get("AlarmHistoryItems", []) or []:
            ts = str(item.get("Timestamp", ""))
            if watermark and ts <= watermark:
                continue
            event = _alarm_history_to_state_change(item, account_id=account_id, region=region)
            if event is None:
                continue
            events.append(event)
            if ts > newest:
                newest = ts
        token = resp.get("NextToken")
        if not token:
            break
    else:
        logger.warning("aws_poll_source: cloudwatch hit MAX_PAGES_PER_POLL for account %s", account_id)
    return events, newest


def read_cloudtrail(
    client: Any, *, region: Optional[str], account_id: str, watermark: str, page_size: int
) -> Tuple[List[Dict[str, Any]], str]:
    """Read new CloudTrail management events since ``watermark``.

    Uses ``cloudtrail:LookupEvents``, following ``NextToken`` internally. Each
    event's ``CloudTrailEvent`` JSON is the record shape map_cloudtrail consumes.
    """
    events: List[Dict[str, Any]] = []
    newest = watermark
    token: Optional[str] = None
    for _ in range(MAX_PAGES_PER_POLL):
        params: Dict[str, Any] = {"MaxResults": page_size}
        if watermark:
            params["StartTime"] = watermark
        if token:
            params["NextToken"] = token
        resp = client.lookup_events(**params) or {}
        for ev in resp.get("Events", []) or []:
            record = _parse_cloudtrail_event(ev)
            if record is None:
                continue
            ts = str(record.get("eventTime", ""))
            if watermark and ts <= watermark:
                continue
            events.append(record)
            if ts > newest:
                newest = ts
        token = resp.get("NextToken")
        if not token:
            break
    else:
        logger.warning("aws_poll_source: cloudtrail hit MAX_PAGES_PER_POLL for account %s", account_id)
    return events, newest


def _parse_cloudtrail_event(ev: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Unwrap a LookupEvents entry to the raw CloudTrail record (map_cloudtrail shape)."""
    raw = ev.get("CloudTrailEvent")
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            logger.warning("aws_poll_source: unparseable CloudTrailEvent — skipped")
            return None
    if isinstance(raw, dict):
        return raw
    # Some fixtures/paths hand the record fields directly on the entry.
    return ev if ev.get("eventID") or ev.get("eventName") else None


def read_eventbridge(
    client: Any, *, region: Optional[str], account_id: str, watermark: str, page_size: int
) -> Tuple[List[Dict[str, Any]], str]:
    """Read the bounded EventBridge rule inventory as operational events.

    Uses ``events:ListRules`` + ``events:DescribeRule`` on the scoped rules — the
    bounded surface reachable through the granted ``events:Describe*/List*`` (the
    bus exposes no past-event read API). Each rule becomes an EventBridge event
    envelope map_eventbridge consumes. The rule set is configuration, so there is
    no time watermark to advance — it is re-observed each run and folded/deduped by
    admission (identical rule arn ⇒ same signal).
    """
    events: List[Dict[str, Any]] = []
    token: Optional[str] = None
    for _ in range(MAX_PAGES_PER_POLL):
        params: Dict[str, Any] = {"Limit": page_size}
        if token:
            params["NextToken"] = token
        resp = client.list_rules(**params) or {}
        for rule in resp.get("Rules", []) or []:
            detail = _describe_rule(client, rule)
            events.append(_rule_to_event(detail, account_id=account_id, region=region))
        token = resp.get("NextToken")
        if not token:
            break
    else:
        logger.warning("aws_poll_source: eventbridge hit MAX_PAGES_PER_POLL for account %s", account_id)
    return events, watermark


def _describe_rule(client: Any, rule: Dict[str, Any]) -> Dict[str, Any]:
    """Enrich a ListRules entry with DescribeRule detail (tolerant; falls back)."""
    name = rule.get("Name")
    if not name or not hasattr(client, "describe_rule"):
        return rule
    try:
        detail = client.describe_rule(Name=name) or {}
    except Exception:  # noqa: BLE001 — a describe failure degrades to the list entry
        logger.debug("aws_poll_source: describe_rule failed for %s — using list entry", name, exc_info=True)
        return rule
    merged = dict(rule)
    merged.update(detail)
    return merged


def _rule_to_event(
    rule: Dict[str, Any], *, account_id: str, region: Optional[str]
) -> Dict[str, Any]:
    """Adapt an EventBridge rule to the map_eventbridge event envelope shape."""
    arn = rule.get("Arn") or ""
    name = rule.get("Name") or "EventBridge Rule"
    return {
        "id": arn or f"eventbridge-rule:{account_id}:{name}",
        "detail-type": name,
        "source": "aws.events",
        "account": account_id,
        "region": region,
        "resources": [arn] if arn else [],
        "detail": {"state": rule.get("State")},
    }


_SURFACE_READERS = {
    SURFACE_CLOUDWATCH: read_cloudwatch,
    SURFACE_EVENTBRIDGE: read_eventbridge,
    SURFACE_CLOUDTRAIL: read_cloudtrail,
}


# ─────────────────────────────────────────────────────────────────────────────
# The poll source
# ─────────────────────────────────────────────────────────────────────────────

class AWSLivePollSource(CloudPollSource):
    """Live AWS :class:`CloudPollSource` — one connection, many accounts (AT-642).

    Builds a scope per ``(account, region, surface)`` from the managed-account
    config, authenticates each account through :class:`AWSAuthenticator`, and reads
    that surface with the minimal read-only API calls. Injectable authenticator
    (hence injectable client factory) so the whole path is exercised in tests with
    seeded fake clients — no boto3, no AWS account.
    """

    def __init__(
        self,
        accounts: List[AWSAccountConfig],
        authenticator: AWSAuthenticator,
        *,
        surfaces: Tuple[str, ...] = AWS_SURFACES,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> None:
        for surface in surfaces:
            if surface not in AWS_SURFACE_MAPPERS:
                raise ValueError(f"unknown AWS surface {surface!r}")
        self.accounts = list(accounts)
        self.authenticator = authenticator
        self.surfaces = tuple(surfaces)
        self.page_size = page_size
        self._by_account: Dict[str, AWSAccountConfig] = {a.account_id: a for a in self.accounts}

    def list_scopes(self, org_id: str) -> List[CloudScope]:
        scopes: List[CloudScope] = []
        for account in self.accounts:
            regions = account.regions or (None,)
            for region in regions:
                for surface in self.surfaces:
                    scopes.append(aws_scope(
                        account.account_id, surface, region=region, label=account.label
                    ))
        return scopes

    def poll(self, org_id: str, scope: CloudScope, position: str) -> PollPage:
        account = self._by_account.get(scope.account)
        if account is None:  # scope for an account we do not manage — nothing to poll
            return PollPage(events=[], next_position=position, has_more=False)

        # Authenticate this account (assume-role, else direct keys). A per-account
        # auth failure degrades that scope to empty rather than crashing the run.
        try:
            credentials: AWSCredentials = self.authenticator.credentials_for(org_id, account)
        except Exception as exc:  # noqa: BLE001 — degrade, don't crash the whole run
            logger.warning(
                "aws_poll_source: skipping scope %s — auth failed: %s",
                scope.scope_key, exc,
            )
            return PollPage(events=[], next_position=position, has_more=False)

        service = _SERVICE_FOR_SURFACE[scope.surface]
        client = self.authenticator.client_factory.client(
            service, region=scope.region, credentials=credentials
        )
        reader = _SURFACE_READERS[scope.surface]
        events, new_watermark = reader(
            client,
            region=scope.region,
            account_id=scope.account,
            watermark=position or "",
            page_size=self.page_size,
        )
        # All within-run pagination is handled inside the reader, so one page per
        # scope; across-run resume rides the returned watermark.
        return PollPage(events=events, next_position=new_watermark, has_more=False)


def build_live_aws_source(
    *,
    accounts: Optional[List[AWSAccountConfig]] = None,
    authenticator: Optional[AWSAuthenticator] = None,
    surfaces: Tuple[str, ...] = AWS_SURFACES,
    page_size: int = DEFAULT_PAGE_SIZE,
    env: Optional[Dict[str, str]] = None,
) -> AWSLivePollSource:
    """Build a live AWS poll source from config (accounts) + a default authenticator.

    ``accounts`` defaults to :func:`aws_auth.load_aws_accounts` (the
    ``AWS_EVENT_ACCOUNTS`` config); ``authenticator`` defaults to a vault-backed
    :class:`AWSAuthenticator` with the real boto3 client factory. Both are
    injectable so tests build a fully-seeded source with no AWS account.
    """
    from .aws_auth import load_aws_accounts

    accts = accounts if accounts is not None else load_aws_accounts(env=env)
    auth = authenticator or AWSAuthenticator()
    return AWSLivePollSource(accts, auth, surfaces=surfaces, page_size=page_size)
