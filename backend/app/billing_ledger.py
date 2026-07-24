"""R-1.9.1-L2 / T2 (AT-694) — the system connect/disconnect billing ledger (AC2).

Emits ``billing.system_connected`` / ``billing.system_disconnected`` into the
immutable telemetry store on each genuine Integration-Hub connect/disconnect, so
the L2 usage report (T3, ``usage_report.py``) has the pro-ration record for
mid-term system additions/removals — a system connected partway through a billing
period, or removed before its end, is billed for the portion it was live.

Design (mirrors the T1 ``billing.run_completed`` emitter in ``discovery/runner.py``):

  * **Transition-gated.** A "system" is one connected Integration-Hub entity — a
    connector whose per-org state is ``status == "connected"`` (the same pricing
    definition ``license_limits.count_connected_systems`` counts). So the ledger
    records only TRUE state transitions: re-authorising an already-connected
    connector is not a new system (emits nothing), and disconnecting a connector
    that was never connected is not a removal (emits nothing). This keeps the
    ledger free of phantom additions/removals so CloudFulcrum's pro-ration maths
    over the raw event list stays correct.

  * **Fire-and-forget.** Every emission is wrapped so a metering failure can never
    break or fail a connect/disconnect request — a failure is logged and swallowed.

  * **seq-stamped (T4).** Each event carries a per-org monotonic ``seq`` from
    ``billing_chain.next_seq`` so a locally-deleted ledger row surfaces as a gap in
    the usage report's tamper-evidence chain. A counter hiccup yields ``seq=None``
    (still emitted, just unsequenced).

  * **PII/secret-safe.** The payload is connector id + a non-secret instance URL +
    a timestamp + the sequence number only. Never a token, credential, or the
    org's private data.

The caller decides direction and supplies ``was_connected`` (the connection state
BEFORE the operation) so the transition can be judged; the routes capture it
before they mutate the connector record / revoke the credential.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from app.telemetry import record_event

logger = logging.getLogger(__name__)

EVENT_SYSTEM_CONNECTED = "billing.system_connected"
EVENT_SYSTEM_DISCONNECTED = "billing.system_disconnected"

# Marks the emitting subsystem on the telemetry row (like T1's "run_pipeline").
_SOURCE = "integration_hub"

CONNECTED_STATUS = "connected"


def is_connected(org_id: str, connector_id: str) -> bool:
    """Whether this connector is currently connected for the org.

    Reads the same per-org connector state ``license_limits`` counts as a
    connected system, so the ledger's notion of a transition matches the pricing
    definition. Defensive: any lookup failure reads as "not connected" so a state
    read can never break the caller.
    """
    try:
        from app import db

        record = db.org_connector_get(org_id, connector_id)
        return bool(record) and record.get("status") == CONNECTED_STATUS
    except Exception:  # pragma: no cover — a state read must never break a request
        logger.warning(
            "billing_ledger.is_connected lookup failed for %s/%s",
            org_id,
            connector_id,
            exc_info=True,
        )
        return False


def resolve_system_identity(org_id: str, connector_id: str) -> str:
    """The concrete system instance being added/removed, for pro-ration.

    A "system" is billed per connected entity, but two orgs — or even one org over
    time — can point the same connector type at different instances, so the ledger
    records the concrete instance where one is known:

      1. the captured OAuth instance/site URL (Salesforce/ServiceNow/Jira/
         Confluence — stored at connect time, and NOT cleared by a token revoke, so
         it is still resolvable on disconnect), else
      2. the static-credential ``base_url`` (Jira token / ServiceNow / native DBs),
         else
      3. the connector id itself — a stable, never-null fallback for connectors
         with no instance URL (Slack, Teams, GitHub).

    Always non-secret and never null. Best-effort: any lookup failure falls through
    to the connector id.
    """
    try:
        from app.live_ingest_credentials import get_connector_instance_url

        url = get_connector_instance_url(org_id, connector_id)
        if url:
            return url
    except Exception:
        logger.debug(
            "billing_ledger: instance-url lookup failed for %s/%s",
            org_id,
            connector_id,
            exc_info=True,
        )
    try:
        from app.auth.vault import get_static_credential_metadata

        meta = get_static_credential_metadata(org_id, connector_id)
        if meta and meta.get("base_url"):
            return str(meta["base_url"])
    except Exception:
        logger.debug(
            "billing_ledger: static base_url lookup failed for %s/%s",
            org_id,
            connector_id,
            exc_info=True,
        )
    return connector_id


def _emit(event_type: str, org_id: str, connector_id: str, system_identity: Optional[str]) -> None:
    """Write one ledger event. Fire-and-forget — never raises to the caller."""
    try:
        identity = system_identity or resolve_system_identity(org_id, connector_id)
        # Per-org monotonic tamper-evidence sequence (T4). Defensive — a counter
        # hiccup yields seq=None (the event is still emitted, just unsequenced).
        try:
            from app import billing_chain

            seq: Optional[int] = billing_chain.next_seq(org_id)
        except Exception:
            seq = None
        payload = {
            "connector": connector_id,
            "system_identity": identity,
            "occurred_at": datetime.now(timezone.utc).isoformat(),
            "org_id": org_id,
            "seq": seq,
            "source": _SOURCE,
        }
        # Attribute the event to the org the transition BELONGS to — not the ambient
        # request context. record_event lets the request context win over the payload
        # org_id (by design), but the OAuth authorization_code callback runs
        # unauthenticated (ambient org = DEV_DEFAULT_ORG) while acting for the org in
        # its signed state nonce. Without pinning, the connect would be filed under
        # "default" while its seq came from THIS org's counter — splitting the
        # tenant's ledger from its sequence and gapping its tamper chain. Correct
        # attribution, not spoofing: the event truly belongs to org_id.
        from app.middleware.tenancy import event_org_context

        with event_org_context(org_id):
            record_event(event_type, payload)
    except Exception:  # pragma: no cover — metering must never break a request
        logger.warning(
            "%s emit failed for %s/%s", event_type, org_id, connector_id, exc_info=True
        )


def emit_system_connected(
    org_id: str,
    connector_id: str,
    *,
    was_connected: bool,
    system_identity: Optional[str] = None,
) -> None:
    """Record a system ADDITION on a genuine not-connected -> connected transition.

    ``was_connected`` is the connection state BEFORE the connect operation. A
    re-authorisation of an already-connected connector (``was_connected=True``) is
    not a new system and emits nothing.
    """
    if was_connected:
        return
    _emit(EVENT_SYSTEM_CONNECTED, org_id, connector_id, system_identity)


def emit_system_disconnected(
    org_id: str,
    connector_id: str,
    *,
    was_connected: bool,
    system_identity: Optional[str] = None,
) -> None:
    """Record a system REMOVAL on a genuine connected -> not-connected transition.

    ``was_connected`` is the connection state BEFORE the disconnect operation.
    Disconnecting a connector that was never connected (``was_connected=False``) is
    idempotent and emits nothing. Disconnect routes resolve ``system_identity``
    before revoking the credential and pass it here, so the removed instance is
    recorded even after a static credential's ``base_url`` is cleared.
    """
    if not was_connected:
        return
    _emit(EVENT_SYSTEM_DISCONNECTED, org_id, connector_id, system_identity)


def record_connection_change(
    org_id: str,
    connector_id: str,
    *,
    was_connected: bool,
    now_connected: bool,
    system_identity: Optional[str] = None,
) -> None:
    """Emit the right ledger event for a bidirectional status change (e.g. the
    generic ``POST /api/connectors/{id}/connect`` toggle in ``main.py``).

    Only a real transition emits: not-connected -> connected records an addition,
    connected -> not-connected records a removal, and a no-op (status unchanged)
    emits nothing.
    """
    if now_connected and not was_connected:
        _emit(EVENT_SYSTEM_CONNECTED, org_id, connector_id, system_identity)
    elif was_connected and not now_connected:
        _emit(EVENT_SYSTEM_DISCONNECTED, org_id, connector_id, system_identity)
