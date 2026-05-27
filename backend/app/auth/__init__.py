from __future__ import annotations

from app.auth.models import (
    ConnectorAuthConfig,
    ConnectorNotAuthenticatedError,
    TokenRecord,
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
    get_client_credentials_token,
    refresh_token,
)

# from app.auth.vault import get_token, store_token, revoke_token  # added in T5

__all__ = [
    "ConnectorAuthConfig",
    "TokenRecord",
    "ConnectorNotAuthenticatedError",
    "MissingSecretError",
    "resolve_secret",
    "validate_all_secrets",
    "OAuthError",
    "build_auth_url",
    "exchange_code",
    "get_client_credentials_token",
    "refresh_token",
]
