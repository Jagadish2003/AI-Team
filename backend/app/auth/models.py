from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Literal, Optional

# ---------------------------------------------------------------------------
# Connector auth mode (R18-A3 T1 / AT-554)
#
# A connector's authentication has a MODE — a concept broader than any single
# OAuth grant. Four modes are recognised:
#
#   authorization_code  — user-delegated OAuth (browser redirect + inbound callback)
#   client_credentials  — service-to-service OAuth (outbound-only; no callback)
#   jwt_bearer          — signed-assertion OAuth (cert in vault; outbound-only)
#   static              — vault-stored static credential (API token / user+password /
#                         native DB connection credentials); no OAuth dance
#
# CRITICAL invariant (AC3): every mode terminates in the SAME vault record shape,
# so downstream ingestion is mode-agnostic — it resolves credentials through the
# unchanged get_connector_credentials(org_id, connector_id) and never branches on
# auth mode. The mode concept lives entirely at the auth EDGE (connect / setup);
# it never leaks into ingestion. See app/auth/auth_modes.py for the per-connector
# supported-modes registry and the per-org selection resolver.
# ---------------------------------------------------------------------------
AuthMode = Literal["authorization_code", "client_credentials", "jwt_bearer", "static"]


@dataclass
class ConnectorAuthConfig:
    """Static OAuth configuration for a connector.

    secret_key holds the ENV VAR NAME (e.g. "SALESFORCE_CLIENT_SECRET"),
    never the actual secret value. See README.md for naming convention.
    """

    connector_id: str
    flow: Literal["authorization_code", "client_credentials"]
    client_id: str
    secret_key: str
    token_url: str
    scopes: List[str]
    revocation_url: Optional[str] = None      # None when connector has no revocation endpoint
    redirect_uri: Optional[str] = None        # None for client_credentials flows
    authorization_url: Optional[str] = None   # None for client_credentials flows; added in AT-75
    # Extra query params merged into the authorization URL. Used to reliably
    # obtain a long-lived REFRESH token (so access tokens auto-refresh instead of
    # expiring for good): e.g. Atlassian needs ``audience``+``prompt=consent`` and
    # Salesforce needs ``prompt=consent`` to (re-)issue a refresh token. Ignored
    # for client_credentials flows (no browser redirect / no refresh token).
    authorize_params: Dict[str, str] = field(default_factory=dict)
    # R18-A3 T1 (AT-554): the auth MODES this connector supports, most-preferred
    # first. The first entry is the connector's default mode (the one used when an
    # org has selected nothing). ``flow`` remains the OAuth grant used by the
    # authorization-code / client-credentials machinery in oauth.py; this field is
    # the broader mode concept a per-org configuration selects from (it may include
    # ``static`` / ``jwt_bearer`` that ``flow`` cannot express). Left empty by
    # default so the interface stays additive; app/auth/auth_modes.py falls back to
    # deriving the default from ``flow`` when it is empty. Blocked follow-ups
    # (AT-555 jwt_bearer, AT-556/AT-557 client_credentials) extend a connector's
    # list here as each mode's flow is built.
    supported_auth_modes: List[AuthMode] = field(default_factory=list)


@dataclass
class TokenRecord:
    """Persisted OAuth token for one org+connector pair.

    access_token will be encrypted at rest via Fernet (implemented in T5).
    expires_at and all datetimes are always UTC.
    """

    org_id: str
    connector_id: str
    access_token: str
    expires_at: datetime
    scopes: List[str]
    created_at: datetime
    updated_at: datetime
    refresh_token: Optional[str] = None  # None for client_credentials flows


@dataclass
class StaticCredentialRecord:
    """Persisted static (non-OAuth) credential for one org+connector pair.

    Second vault record type (R17-D3 Addendum A, T10) alongside TokenRecord:
    Jira API tokens, ServiceNow username/password, and native DB connection
    credentials are entered once by an admin — no OAuth dance, no refresh, no
    expiry. Same per-(org_id, connector_id) keying and the same Fernet
    encryption at rest as token records: username/secret are stored encrypted
    (enc_username/enc_secret columns) and this record carries the decrypted
    values at use time only.

    username and secret are excluded from repr so the record can never leak
    plaintext credentials into logs or error messages (AC10: values are
    write-only — never readable back through the UI or logs).
    base_url is the non-secret instance location (e.g. the Jira/ServiceNow
    URL or a DB host); all datetimes are always UTC.
    """

    org_id: str
    connector_id: str
    username: str = field(repr=False)
    secret: str = field(repr=False)
    base_url: str
    created_at: datetime
    updated_at: datetime
    kind: Literal["static"] = "static"


class ConnectorNotAuthenticatedError(Exception):
    """Raised by get_token() (T5) when no valid token exists for the given org+connector.

    Callers should return HTTP 401 when this is caught at the route layer.
    """

    def __init__(self, org_id: str, connector_id: str) -> None:
        self.org_id = org_id
        self.connector_id = connector_id
        super().__init__(
            f"No valid token for connector '{connector_id}' in org '{org_id}'"
        )
