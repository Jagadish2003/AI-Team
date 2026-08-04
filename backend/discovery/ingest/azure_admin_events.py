"""
azure_admin_events.py — MSP-B2 T3 (AT-650): Activity Log + Service Health polling.

The transport edge for the other two V1 Azure event classes (Azure Monitor Alerts
is T2, ``azure_alerts.py``):

  * Azure **Activity Log** — ADMINISTRATIVE events only (the audit stream), via the
    Azure Monitor Activity Log REST surface. Normalised by ``map_azure_activity_log``.
  * Azure **Service Health** — service issue / maintenance / advisory events, via the
    Resource Health events surface. Normalised by ``map_service_health``.

Scope defence (MSP-B2 §"SCOPE DEFENCE" / T3-AC4): the Activity Log poller keeps
ONLY ``category = Administrative`` records — ServiceHealth/Security/Policy/
Recommendation/Autoscale/Alert/ResourceHealth categories are dropped here and never
ingested through this path. Metrics, Log Analytics, diagnostic logs, and Defender/
Sentinel are not touched at all.

Mirrors ``azure_alerts.py`` exactly (injectable client → fixture offline / live
outbound-only GET; field accessors used by the connector's generic per-subscription
stream loop). Outbound-only: a single GET per subscription, no webhook/listener, so
it is honoured under ``NETWORK_PROFILE=no_public_inbound``. No detector-visible
fields are invented — raw records pass to the B0 mappers.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Protocol

try:
    from . import is_live
except Exception:  # pragma: no cover - import shim
    from discovery.ingest import is_live

from app.azure_environments import AzureEnvironment

from .azure_app_insights import assert_read_allowed

logger = logging.getLogger(__name__)

# API versions (non-secret; overridable per client).
ACTIVITY_LOG_API_VERSION = "2015-04-01"
SERVICE_HEALTH_API_VERSION = "2018-07-01"

_ACTIVITY_LOG_PATH = "providers/Microsoft.Insights/eventtypes/management/values"
_SERVICE_HEALTH_PATH = "providers/Microsoft.ResourceHealth/events"

#: Activity Log ``$filter`` window. The Activity Log List operation REQUIRES a
#: ``$filter`` carrying at least a start ``eventTimestamp`` — a request without one
#: is answered HTTP 400 by ARM, not an empty page. A first poll (no checkpoint yet)
#: therefore still needs an explicit start bound.
ACTIVITY_LOG_DEFAULT_LOOKBACK_DAYS = 7
#: Azure retains Activity Log events for 90 days; ARM rejects a start bound older
#: than that, so a stale checkpoint is clamped to the retention floor.
ACTIVITY_LOG_MAX_LOOKBACK_DAYS = 90

#: The ONLY Activity Log category this connector ingests (scope defence / AC4).
ADMINISTRATIVE_CATEGORY = "Administrative"

ACTIVITY_LOG_FIXTURE = Path(__file__).parent / "fixtures" / "azure_activity_log_sample.json"
SERVICE_HEALTH_FIXTURE = Path(__file__).parent / "fixtures" / "azure_service_health_sample.json"


# ── shared helpers ───────────────────────────────────────────────────────────


def _subscription_from_ids(*values: Any) -> str:
    """Extract a subscription id from any ``/subscriptions/{id}/...`` string given."""
    marker = "/subscriptions/"
    for value in values:
        text = str(value or "")
        idx = text.lower().find(marker)
        if idx != -1:
            sub = text[idx + len(marker):].split("/", 1)[0].strip()
            if sub:
                return sub
    return ""


def _value_of(field: Any) -> str:
    """Azure fields are often ``{'value': X, 'localizedValue': Y}`` — take value."""
    if isinstance(field, dict):
        return str(field.get("value") or "")
    return str(field or "")


# ── Activity Log (administrative) accessors ─────────────────────────────────────


def activity_id(raw: Dict[str, Any]) -> str:
    return str((raw or {}).get("eventDataId") or (raw or {}).get("correlationId") or "")


def activity_timestamp(raw: Dict[str, Any]) -> str:
    return str((raw or {}).get("eventTimestamp") or "")


def activity_category(raw: Dict[str, Any]) -> str:
    return _value_of((raw or {}).get("category"))


def activity_subscription_id(raw: Dict[str, Any]) -> str:
    return _subscription_from_ids(
        (raw or {}).get("subscriptionId"),
        (raw or {}).get("resourceId"),
        (raw or {}).get("id"),
    )


def is_administrative(raw: Dict[str, Any]) -> bool:
    """True when the Activity Log record is an ADMINISTRATIVE event (scope defence).

    A record whose category is explicitly a non-Administrative value is excluded.
    A record with no category is treated as administrative — the connector queries
    the Activity Log *management* (administrative) surface, so an absent category
    on that surface is administrative by construction; an explicit foreign category
    (ServiceHealth/Security/Policy/…) is dropped.
    """
    cat = activity_category(raw).strip().lower()
    return cat == "" or cat == ADMINISTRATIVE_CATEGORY.lower()


# ── Activity Log $filter construction (REQUIRED by ARM) ─────────────────────────


def _parse_iso(value: Any) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp to an aware UTC datetime, or None."""
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso_z(moment: datetime) -> str:
    """Render an aware datetime in the form ARM accepts (UTC, trailing ``Z``)."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def build_activity_log_filter(
    since_iso: Optional[str],
    *,
    now: Optional[datetime] = None,
    lookback_days: int = ACTIVITY_LOG_DEFAULT_LOOKBACK_DAYS,
) -> str:
    """Build the ``$filter`` the Activity Log List operation REQUIRES.

    ``$filter`` is a *required* query parameter on
    ``GET /subscriptions/{id}/providers/Microsoft.Insights/eventtypes/management/values``
    and must carry at least a start ``eventTimestamp``; without it ARM answers
    HTTP 400 (Azure Monitor "Activity Logs - List", api-version 2015-04-01). ARM
    also accepts only a fixed set of filter shapes — this emits the documented
    *"list events for a subscription in a time range"* form::

        eventTimestamp ge '<start>' and eventTimestamp le '<end>'

    Start bound: the subscription's checkpoint (``since_iso``) when there is one,
    otherwise ``now - lookback_days``. Either way it is clamped to the 90-day
    Activity Log retention floor, because ARM also rejects a start bound older
    than retention — which would otherwise reintroduce the same 400 once a
    checkpoint aged out.

    ``ge`` is inclusive, so the boundary record can be returned again; the
    connector's strictly-newer ``_filter_new`` drops it, leaving incremental
    semantics identical to the offline path.
    """
    end = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    floor = end - timedelta(days=ACTIVITY_LOG_MAX_LOOKBACK_DAYS)

    start = _parse_iso(since_iso)
    if start is None:
        if since_iso:
            logger.warning(
                "azure_admin_events: unparseable activity-log checkpoint %r — "
                "falling back to a %d-day window",
                since_iso, lookback_days,
            )
        start = end - timedelta(days=max(1, lookback_days))
    if start < floor:
        start = floor
    if start > end:  # checkpoint ahead of our clock — keep the window valid
        start = end

    return f"eventTimestamp ge '{_iso_z(start)}' and eventTimestamp le '{_iso_z(end)}'"


def activity_log_params(since_iso: Optional[str]) -> Dict[str, str]:
    """The Activity-Log-specific query parameters (the required ``$filter``)."""
    return {"$filter": build_activity_log_filter(since_iso)}


# ── Service Health accessors ────────────────────────────────────────────────────


def _service_health_props(raw: Dict[str, Any]) -> Dict[str, Any]:
    props = (raw or {}).get("properties")
    return props if isinstance(props, dict) else {}


def service_health_id(raw: Dict[str, Any]) -> str:
    props = _service_health_props(raw)
    return str(
        props.get("trackingId")
        or (raw or {}).get("eventDataId")
        or (raw or {}).get("correlationId")
        or (raw or {}).get("name")
        or ""
    )


def service_health_timestamp(raw: Dict[str, Any]) -> str:
    return str(
        (raw or {}).get("eventTimestamp")
        or _service_health_props(raw).get("impactStartTime")
        or ""
    )


def service_health_subscription_id(raw: Dict[str, Any]) -> str:
    return _subscription_from_ids(
        (raw or {}).get("subscriptionId"),
        (raw or {}).get("id"),
        (raw or {}).get("resourceId"),
    )


# ── client contracts ─────────────────────────────────────────────────────────


class AzureEventStreamClient(Protocol):
    """Fetches raw records for one Azure event stream in a subscription."""

    def fetch(
        self,
        *,
        token: str,
        subscription_id: str,
        environment: AzureEnvironment,
        since_iso: Optional[str],
    ) -> List[Dict[str, Any]]:
        ...


class _FixtureStreamClient:
    """Offline client reading a fixture and filtering to one subscription."""

    def __init__(self, fixture_path: Path, subscription_of) -> None:
        self._fixture_path = fixture_path
        self._subscription_of = subscription_of

    def fetch(self, *, token, subscription_id, environment, since_iso):
        if not self._fixture_path.exists():
            return []
        with open(self._fixture_path, encoding="utf-8") as fh:
            data = json.load(fh)
        records = list(data.get("value", []) if isinstance(data, dict) else [])
        return [r for r in records if self._subscription_of(r) == str(subscription_id)]


class _HttpStreamClient:
    """Live client: a single outbound ARM GET (Reader-only, no listener)."""

    def __init__(
        self,
        *,
        path: str,
        api_version: str,
        timeout_seconds: int = 30,
        transport: Any = None,
        params_builder: Optional[Callable[[Optional[str]], Dict[str, str]]] = None,
        stream: str = "",
    ) -> None:
        self._path = path
        self._api_version = api_version
        self._timeout = timeout_seconds
        self._transport = transport
        # Per-stream extra query parameters. Only the Activity Log supplies one
        # (its REQUIRED $filter); Alerts and Service Health pass none, so their
        # requests are unchanged.
        self._params_builder = params_builder
        # The stream key, for LOG TEXT ONLY (never sent to ARM, never on a record).
        self._stream = stream or path

    def fetch(self, *, token, subscription_id, environment, since_iso):
        import httpx  # local import so offline runs never require httpx at import

        url = (
            f"{environment.resource_manager.rstrip('/')}/subscriptions/"
            f"{subscription_id}/{self._path}"
        )
        # 2.0-D3 T1 / AC2: refuse an out-of-scope surface at the point of the call,
        # so the scope commitment holds even if this generic client is later
        # re-pointed at metrics, telemetry, or a Log Analytics/KQL endpoint.
        assert_read_allowed(url)
        params = {"api-version": self._api_version}
        if self._params_builder is not None:
            params.update(self._params_builder(since_iso))
        # DEBUG transport trace. The URL and $filter are non-secret request
        # configuration and are the two things that explain a legitimate 200/empty
        # (a clamped or collapsed time window). The bearer token lives only in the
        # headers dict below and is NEVER logged, here or anywhere.
        logger.debug(
            "azure_admin_events: request stream=%s subscription=%s url=%s "
            "api-version=%s filter=%s",
            self._stream, subscription_id, url, self._api_version,
            params.get("$filter", "(none)"),
        )
        with httpx.Client(timeout=self._timeout, transport=self._transport) as client:
            resp = client.get(
                url,
                params=params,
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            )
        resp.raise_for_status()
        body = resp.json()
        records = list(body.get("value", []) if isinstance(body, dict) else [])
        logger.debug(
            "azure_admin_events: response stream=%s subscription=%s status=%s records=%d",
            self._stream, subscription_id, resp.status_code, len(records),
        )
        return records


def default_activity_log_client() -> AzureEventStreamClient:
    """Offline fixture client, or the live Activity Log HTTP client when live."""
    if is_live():
        return _HttpStreamClient(
            path=_ACTIVITY_LOG_PATH,
            api_version=ACTIVITY_LOG_API_VERSION,
            params_builder=activity_log_params,  # $filter is REQUIRED by this operation
            stream="activity_log",
        )
    return _FixtureStreamClient(ACTIVITY_LOG_FIXTURE, activity_subscription_id)


def default_service_health_client() -> AzureEventStreamClient:
    """Offline fixture client, or the live Service Health HTTP client when live."""
    if is_live():
        return _HttpStreamClient(
            path=_SERVICE_HEALTH_PATH,
            api_version=SERVICE_HEALTH_API_VERSION,
            stream="service_health",
        )
    return _FixtureStreamClient(SERVICE_HEALTH_FIXTURE, service_health_subscription_id)
