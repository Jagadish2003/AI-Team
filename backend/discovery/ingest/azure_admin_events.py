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
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol

try:
    from . import is_live
except Exception:  # pragma: no cover - import shim
    from discovery.ingest import is_live

from app.azure_environments import AzureEnvironment

logger = logging.getLogger(__name__)

# API versions (non-secret; overridable per client).
ACTIVITY_LOG_API_VERSION = "2015-04-01"
SERVICE_HEALTH_API_VERSION = "2018-07-01"

_ACTIVITY_LOG_PATH = "providers/Microsoft.Insights/eventtypes/management/values"
_SERVICE_HEALTH_PATH = "providers/Microsoft.ResourceHealth/events"

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

    def __init__(self, *, path: str, api_version: str, timeout_seconds: int = 30, transport: Any = None) -> None:
        self._path = path
        self._api_version = api_version
        self._timeout = timeout_seconds
        self._transport = transport

    def fetch(self, *, token, subscription_id, environment, since_iso):
        import httpx  # local import so offline runs never require httpx at import

        url = (
            f"{environment.resource_manager.rstrip('/')}/subscriptions/"
            f"{subscription_id}/{self._path}"
        )
        params = {"api-version": self._api_version}
        with httpx.Client(timeout=self._timeout, transport=self._transport) as client:
            resp = client.get(
                url,
                params=params,
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            )
        resp.raise_for_status()
        body = resp.json()
        return list(body.get("value", []) if isinstance(body, dict) else [])


def default_activity_log_client() -> AzureEventStreamClient:
    """Offline fixture client, or the live Activity Log HTTP client when live."""
    if is_live():
        return _HttpStreamClient(path=_ACTIVITY_LOG_PATH, api_version=ACTIVITY_LOG_API_VERSION)
    return _FixtureStreamClient(ACTIVITY_LOG_FIXTURE, activity_subscription_id)


def default_service_health_client() -> AzureEventStreamClient:
    """Offline fixture client, or the live Service Health HTTP client when live."""
    if is_live():
        return _HttpStreamClient(path=_SERVICE_HEALTH_PATH, api_version=SERVICE_HEALTH_API_VERSION)
    return _FixtureStreamClient(SERVICE_HEALTH_FIXTURE, service_health_subscription_id)
