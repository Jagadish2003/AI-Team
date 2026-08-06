"""
azure_alerts.py — MSP-B2 T2 (AT-649): Azure Monitor Alerts polling.

The transport edge for ONE of the three V1 Azure event classes — Azure Monitor
ALERTS (fired alert instances via the Alerts Management API). Scope-defence
(MSP-B2 §"SCOPE DEFENCE"): this module reads Alerts Management ONLY. It does NOT
touch Activity Log, Service Health (both MSP-B2 T3), metrics, Log Analytics,
diagnostic logs, or Defender/Sentinel.

Design:
  * An injectable ``AzureAlertsClient`` fetches the raw alert page for a
    subscription (live: outbound-only ARM GET; offline: the deterministic
    fixture). The connector (``azure_events.py``) owns auth, the pinned
    subscription set, checkpoints, mapping (``map_azure_monitor``), and emission —
    this module owns only "get the raw alerts for a subscription".
  * Incremental by per-subscription checkpoint: :func:`filter_new_alerts` keeps
    only alerts strictly newer than the subscription's last-seen ``firedDateTime``,
    so a second run re-reads nothing (T2-AC2/AC4). :func:`max_fired_at` gives the
    connector the value to advance that subscription's checkpoint to.

Outbound-only (MSP-B2 §"Transport"): the live client performs a single GET and no
webhook/listener, so it is honoured under ``NETWORK_PROFILE=no_public_inbound``.
No detector-visible fields are invented here — raw alerts pass to the B0 mapper.
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

from .azure_app_insights import assert_read_allowed

logger = logging.getLogger(__name__)

#: Alerts Management REST API version (non-secret). Overridable per client.
ALERTS_API_VERSION = "2019-05-05-preview"

#: The Alerts Management list path under a subscription.
_ALERTS_PATH = "providers/Microsoft.AlertsManagement/alerts"

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "azure_monitor_alerts_sample.json"


# ── raw-alert field helpers (Azure common alert schema) ─────────────────────────


def _essentials(raw: Dict[str, Any]) -> Dict[str, Any]:
    return ((raw or {}).get("data") or {}).get("essentials") or {}


def alert_id(raw: Dict[str, Any]) -> str:
    """The stable provider id of an alert (essentials.alertId)."""
    return str(_essentials(raw).get("alertId") or "")


def alert_fired_at(raw: Dict[str, Any]) -> str:
    """The alert's fired timestamp (essentials.firedDateTime), or ''."""
    return str(_essentials(raw).get("firedDateTime") or "")


def alert_subscription_id(raw: Dict[str, Any]) -> str:
    """Extract the subscription id from an alert's id / target (best-effort)."""
    ess = _essentials(raw)
    candidates = [ess.get("alertId") or ""]
    targets = ess.get("alertTargetIDs") or []
    if isinstance(targets, list):
        candidates.extend(str(t) for t in targets)
    for value in candidates:
        marker = "/subscriptions/"
        low = str(value)
        idx = low.lower().find(marker)
        if idx != -1:
            rest = low[idx + len(marker):]
            sub = rest.split("/", 1)[0].strip()
            if sub:
                return sub
    return ""


def filter_new_alerts(alerts: List[Dict[str, Any]], since_iso: Optional[str]) -> List[Dict[str, Any]]:
    """Return only alerts fired strictly after ``since_iso`` (incremental read).

    ISO-8601 firedDateTime values sort lexicographically in chronological order
    (UTC, zero-padded, trailing ``Z``), so a string comparison is a correct time
    comparison here without parsing. ``since_iso`` None/'' means "first run — take
    everything". An alert with no firedDateTime is kept (it cannot be proven old),
    so nothing is silently dropped.
    """
    if not since_iso:
        return list(alerts)
    out: List[Dict[str, Any]] = []
    for a in alerts:
        fired = alert_fired_at(a)
        if not fired or fired > since_iso:
            out.append(a)
    return out


def max_fired_at(alerts: List[Dict[str, Any]], *, floor: Optional[str] = None) -> Optional[str]:
    """The maximum firedDateTime across ``alerts`` (never below ``floor``).

    The value the connector advances a subscription's checkpoint to. Returns
    ``floor`` (the existing checkpoint) when there are no dated alerts, so an
    empty poll never regresses or clears a checkpoint.
    """
    best = floor or None
    for a in alerts:
        fired = alert_fired_at(a)
        if fired and (best is None or fired > best):
            best = fired
    return best


# ── the client contract ─────────────────────────────────────────────────────────


class AzureAlertsClient(Protocol):
    """Fetches raw Azure Monitor alerts for a subscription (transport only)."""

    def fetch_alerts(
        self,
        *,
        token: str,
        subscription_id: str,
        environment: AzureEnvironment,
        since_iso: Optional[str],
    ) -> List[Dict[str, Any]]:
        ...


class FixtureAzureAlertsClient:
    """Offline client: reads the deterministic fixture and filters by subscription.

    Returns the fixture alerts whose id/target resolves to ``subscription_id`` so a
    multi-subscription offline run behaves like distinct subscriptions. Never makes
    a network call.
    """

    def __init__(self, fixture_path: Optional[Path] = None) -> None:
        self._fixture_path = fixture_path or FIXTURE_PATH

    def fetch_alerts(
        self,
        *,
        token: str,
        subscription_id: str,
        environment: AzureEnvironment,
        since_iso: Optional[str],
    ) -> List[Dict[str, Any]]:
        if not self._fixture_path.exists():
            return []
        with open(self._fixture_path, encoding="utf-8") as fh:
            data = json.load(fh)
        alerts = list(data.get("value", []) if isinstance(data, dict) else [])
        return [a for a in alerts if alert_subscription_id(a) == str(subscription_id)]


class HttpAzureAlertsClient:
    """Live client: a single outbound ARM Alerts Management GET (no listener).

    Reader-only (Microsoft.AlertsManagement/alerts/read). The subscription's
    fired-time filter is applied client-side via :func:`filter_new_alerts` after
    the fetch, so the checkpoint semantics are identical to the offline path.
    An injectable httpx transport keeps it unit-testable with no network.
    """

    def __init__(
        self,
        *,
        api_version: str = ALERTS_API_VERSION,
        timeout_seconds: int = 30,
        transport: Any = None,
    ) -> None:
        self._api_version = api_version
        self._timeout = timeout_seconds
        self._transport = transport

    def fetch_alerts(
        self,
        *,
        token: str,
        subscription_id: str,
        environment: AzureEnvironment,
        since_iso: Optional[str],
    ) -> List[Dict[str, Any]]:
        import httpx  # local import so offline runs never require httpx at import

        url = (
            f"{environment.resource_manager.rstrip('/')}/subscriptions/"
            f"{subscription_id}/{_ALERTS_PATH}"
        )
        # 2.0-D3 T1 / AC2: refuse an out-of-scope surface at the point of the call.
        # The URL here is a constant, so this can only fire if someone later
        # re-points this client at telemetry, metrics, or a Log Analytics/KQL
        # endpoint — which is exactly when a scope commitment needs to hold.
        assert_read_allowed(url)
        params = {"api-version": self._api_version}
        # timeRange caps the server-side window; the precise since filter is applied
        # client-side (ARM's alert filters are coarse). Kept modest and outbound.
        with httpx.Client(timeout=self._timeout, transport=self._transport) as client:
            resp = client.get(
                url,
                params=params,
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            )
        resp.raise_for_status()
        body = resp.json()
        return list(body.get("value", []) if isinstance(body, dict) else [])


def default_alerts_client() -> AzureAlertsClient:
    """The offline fixture client, or the live HTTP client when INGEST_MODE=live."""
    return HttpAzureAlertsClient() if is_live() else FixtureAzureAlertsClient()
