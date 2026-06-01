from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backend.app.auth.models import ConnectorAuthConfig


class MissingSecretError(Exception):
    """Raised when a required env var secret is absent.

    Message contains the key NAME only — never the secret value.
    """

    def __init__(self, secret_key: str) -> None:
        self.secret_key = secret_key
        super().__init__(
            f"Required secret '{secret_key}' is not set. "
            "Application cannot start with missing credentials."
        )


def resolve_secret(secret_key: str) -> str:
    """Read a secret from os.environ at call time only.

    Return value must be used immediately — never stored, cached, or logged.
    Raises MissingSecretError if the env var is absent.
    """
    value = os.environ.get(secret_key)
    if value is None:
        raise MissingSecretError(secret_key)
    return value


def validate_all_secrets(connector_configs: dict) -> None:
    """Fail-fast startup check.

    Iterates every ConnectorAuthConfig in connector_configs and checks that its
    secret_key env var is present in os.environ. Raises MissingSecretError on
    the first absent key. Call once at application startup to prevent the app
    starting in a misconfigured state.
    """
    for config in connector_configs.values():
        if os.environ.get(config.secret_key) is None:
            raise MissingSecretError(config.secret_key)
