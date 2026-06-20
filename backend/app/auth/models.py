from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Literal, Optional


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
