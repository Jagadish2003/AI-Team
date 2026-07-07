"""R18-A3 T1 (AT-554) — connector auth-mode abstraction.

Gives connector authentication a MODE concept independent of any single OAuth
grant, and a per-org SELECTION of that mode. Four modes are recognised:

    authorization_code  — user-delegated OAuth (browser redirect + inbound callback)
    client_credentials  — service-to-service OAuth (outbound-only; no callback)
    jwt_bearer          — signed-assertion OAuth (cert in vault; outbound-only)
    static              — vault-stored static credential (API token / user+password /
                          native DB connection credentials); no OAuth dance

Why this exists
---------------
For a server reading data under a service identity, authorization-code-with-
callback was never the natural grant — it exists for user-delegated access and
requires a public inbound HTTPS callback. In no-public-inbound deployments (TCU
and other security-conscious banks) that callback can never arrive, so
authorization-code fails at the last step. client_credentials / jwt_bearer /
static are outbound-only and are the correct grants for service-to-service
ingestion. This module lets each connector declare which modes it supports and
lets a per-org configuration select one.

The one invariant that keeps the change contained (AC3)
-------------------------------------------------------
Every mode terminates in the SAME vault record shape. Downstream ingestion is
therefore mode-AGNOSTIC: it resolves credentials through the unchanged

    creds = get_connector_credentials(org_id, connector_id)   # app/auth/credentials.py

and never branches on auth mode. The mode concept lives entirely at the auth
EDGE (connect / setup); it must never leak into ingestion. The blocked
follow-ups build the specific new flows and register their mode here:

    AT-555  Salesforce (+ nCino) jwt_bearer
    AT-556  Microsoft Graph (Teams / SharePoint) client_credentials
    AT-557  ServiceNow client_credentials

Until a mode's flow is built, it is deliberately NOT listed as supported for a
connector — a connector's supported set reflects what actually works today, so
an org can never select a mode that has no flow behind it.
"""
from __future__ import annotations

from typing import Tuple

from app import db
from app.auth.configs import CONNECTOR_AUTH_CONFIGS
from app.auth.models import AuthMode

# --- Mode constants (avoid stringly-typed drift across the codebase) ---------
AUTH_MODE_AUTHORIZATION_CODE: AuthMode = "authorization_code"
AUTH_MODE_CLIENT_CREDENTIALS: AuthMode = "client_credentials"
AUTH_MODE_JWT_BEARER: AuthMode = "jwt_bearer"
AUTH_MODE_STATIC: AuthMode = "static"

#: Every recognised auth mode.
ALL_AUTH_MODES: frozenset[AuthMode] = frozenset(
    {
        AUTH_MODE_AUTHORIZATION_CODE,
        AUTH_MODE_CLIENT_CREDENTIALS,
        AUTH_MODE_JWT_BEARER,
        AUTH_MODE_STATIC,
    }
)

#: Modes that complete WITHOUT any inbound HTTPS callback — the ones usable in a
#: NETWORK_PROFILE=no_public_inbound deployment (R18-A3 §2; the UI behaviour that
#: consumes this is T5/AT-... , not this task). authorization_code is the only
#: mode that needs the provider to redirect inbound to AgentIQ.
OUTBOUND_ONLY_MODES: frozenset[AuthMode] = frozenset(
    {
        AUTH_MODE_CLIENT_CREDENTIALS,
        AUTH_MODE_JWT_BEARER,
        AUTH_MODE_STATIC,
    }
)

#: Key under which the selected mode is persisted in an org's connector record
#: (the same per-(org_id, connector_id) row db.org_connector_get/set manage).
_AUTH_MODE_FIELD = "auth_mode"

# Connectors that authenticate ONLY with a static credential and therefore have
# no OAuth ConnectorAuthConfig (native DB connectors). Mirrors the ids the static
# credential entry route accepts (routes_connector_auth.STATIC_CREDENTIAL_CONNECTORS);
# their supported set cannot be read off a config object, so it is declared here.
_STATIC_ONLY_SUPPORTED_MODES: dict[str, Tuple[AuthMode, ...]] = {
    "postgresql": (AUTH_MODE_STATIC,),
    "sql_server": (AUTH_MODE_STATIC,),
    "sqlserver": (AUTH_MODE_STATIC,),
    "oracle_db": (AUTH_MODE_STATIC,),
}


class UnknownConnectorError(ValueError):
    """Raised when a connector id has no registered auth modes at all."""

    def __init__(self, connector_id: str) -> None:
        self.connector_id = connector_id
        super().__init__(
            f"Connector '{connector_id}' has no registered auth modes "
            "(unknown connector)."
        )


class UnsupportedAuthModeError(ValueError):
    """Raised when a caller selects a mode a connector does not support."""

    def __init__(
        self, connector_id: str, mode: str, supported: Tuple[AuthMode, ...]
    ) -> None:
        self.connector_id = connector_id
        self.mode = mode
        self.supported = supported
        super().__init__(
            f"Connector '{connector_id}' does not support auth mode '{mode}'. "
            f"Supported: {', '.join(supported) or '(none)'}."
        )


def get_supported_auth_modes(connector_id: str) -> Tuple[AuthMode, ...]:
    """Return the auth modes a connector supports, most-preferred first.

    Single source of truth: an OAuth connector's supported set is declared on its
    ``ConnectorAuthConfig.supported_auth_modes``; a static-only connector (native
    DB) is declared in the module-local map. When a config predates the
    ``supported_auth_modes`` field (empty), the default is derived from its
    ``flow`` so the abstraction still returns a sensible mode.

    Returns an empty tuple for an unknown connector — callers that require a mode
    should raise :class:`UnknownConnectorError` (see :func:`get_default_auth_mode`).
    """
    config = CONNECTOR_AUTH_CONFIGS.get(connector_id)
    if config is not None:
        if config.supported_auth_modes:
            return tuple(config.supported_auth_modes)
        # Backward-compatible fallback: a config with no explicit modes still has
        # a ``flow`` (authorization_code | client_credentials) — treat that as its
        # single supported mode.
        return (config.flow,)  # type: ignore[return-value]
    return _STATIC_ONLY_SUPPORTED_MODES.get(connector_id, ())


def connector_supports_mode(connector_id: str, mode: str) -> bool:
    """True when ``mode`` is one of ``connector_id``'s supported auth modes."""
    return mode in get_supported_auth_modes(connector_id)


def get_default_auth_mode(connector_id: str) -> AuthMode:
    """Return a connector's DEFAULT auth mode (its first/most-preferred one).

    Raises :class:`UnknownConnectorError` for a connector with no registered modes.
    """
    supported = get_supported_auth_modes(connector_id)
    if not supported:
        raise UnknownConnectorError(connector_id)
    return supported[0]


def resolve_auth_mode(org_id: str, connector_id: str) -> AuthMode:
    """Return the auth mode CONFIGURED for this org+connector.

    Reads the per-org selection persisted in the org's connector record and
    validates it against the connector's supported set; falls back to the
    connector default when nothing valid is selected (never selected, or a
    previously selected mode has since been retired). Never raises for a known
    connector — a stale/invalid stored value degrades to the default rather than
    breaking setup. Raises :class:`UnknownConnectorError` only for a connector
    with no registered modes.

    This is a SETUP/connect-time helper. Ingestion does NOT call it: ingestion
    resolves credentials mode-agnostically via get_connector_credentials() (AC3).
    """
    supported = get_supported_auth_modes(connector_id)
    if not supported:
        raise UnknownConnectorError(connector_id)

    selected = None
    try:
        record = db.org_connector_get(org_id, connector_id) or {}
        selected = record.get(_AUTH_MODE_FIELD)
    except Exception:
        # A per-org config read problem must not break mode resolution — fall
        # back to the connector default (a valid, working mode by construction).
        selected = None

    if selected in supported:
        return selected  # type: ignore[return-value]
    return supported[0]


def set_auth_mode(org_id: str, connector_id: str, mode: str) -> AuthMode:
    """Persist an org's auth-mode selection for a connector; return it.

    Validates that the connector supports the mode (a connector's supported set
    reflects what actually has a flow behind it, so this rejects selecting a mode
    with no implementation) and stores it on the org's connector record without
    disturbing its other state (status, lastSynced, ...).

    Raises :class:`UnknownConnectorError` for an unknown connector and
    :class:`UnsupportedAuthModeError` for a mode the connector does not support.
    """
    supported = get_supported_auth_modes(connector_id)
    if not supported:
        raise UnknownConnectorError(connector_id)
    if mode not in supported:
        raise UnsupportedAuthModeError(connector_id, mode, supported)

    record = db.org_connector_get(org_id, connector_id) or {}
    record[_AUTH_MODE_FIELD] = mode
    db.org_connector_set(org_id, connector_id, record)
    return mode  # type: ignore[return-value]
