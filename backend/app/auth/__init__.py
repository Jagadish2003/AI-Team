from __future__ import annotations

from app.auth.models import (
    AuthMode,
    ConnectorAuthConfig,
    ConnectorNotAuthenticatedError,
    TokenRecord,
)
from app.auth.auth_modes import (
    ALL_AUTH_MODES,
    AUTH_MODE_AUTHORIZATION_CODE,
    AUTH_MODE_CLIENT_CREDENTIALS,
    AUTH_MODE_JWT_BEARER,
    AUTH_MODE_STATIC,
    OUTBOUND_ONLY_MODES,
    UnknownConnectorError,
    UnsupportedAuthModeError,
    connector_supports_mode,
    get_default_auth_mode,
    get_supported_auth_modes,
    resolve_auth_mode,
    set_auth_mode,
)
from app.auth.secrets import (
    MissingSecretError,
    resolve_secret,
    validate_all_secrets,
)
from app.auth.oauth import (
    OAuthError,
    build_auth_url,
    exchange_code,
    generate_pkce_pair,
    get_client_credentials_token,
    refresh_token,
)
from app.auth.vault import get_token, revoke_token, store_token
from app.auth.configs import CONNECTOR_AUTH_CONFIGS

__all__ = [
    "ConnectorAuthConfig",
    "AuthMode",
    "ALL_AUTH_MODES",
    "OUTBOUND_ONLY_MODES",
    "AUTH_MODE_AUTHORIZATION_CODE",
    "AUTH_MODE_CLIENT_CREDENTIALS",
    "AUTH_MODE_JWT_BEARER",
    "AUTH_MODE_STATIC",
    "UnknownConnectorError",
    "UnsupportedAuthModeError",
    "connector_supports_mode",
    "get_default_auth_mode",
    "get_supported_auth_modes",
    "resolve_auth_mode",
    "set_auth_mode",
    "TokenRecord",
    "ConnectorNotAuthenticatedError",
    "MissingSecretError",
    "resolve_secret",
    "validate_all_secrets",
    "OAuthError",
    "build_auth_url",
    "exchange_code",
    "generate_pkce_pair",
    "get_client_credentials_token",
    "refresh_token",
    "get_token",
    "store_token",
    "revoke_token",
    "CONNECTOR_AUTH_CONFIGS",
]
