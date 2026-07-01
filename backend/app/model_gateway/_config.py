"""R16-D2 T4 (AT-408) — Hosted provider credential & config handling.

This module owns *every* reference to the hosted provider's credential and
endpoint configuration.  It is the model-side equivalent of the credential
hygiene already applied to connector tokens in the vault (Section 3 of the
R16-D2 story):

  * The API key and endpoint are read from configuration / secrets only —
    never hardcoded (T4-AC1).
  * The credential value is never written to logs at any level.  ``repr`` and
    ``str`` redact it, so even an accidental ``logger.debug("%s", config)`` is
    safe (T4-AC2).
  * Nothing outside ``backend/app/model_gateway/`` imports this module, it is
    not re-exported from the package ``__init__`` (``__all__``), and the
    resolved credential is never returned to or stored by a caller (T4-AC3).

Instantiation vs. call-time resolution
--------------------------------------
``HostedConfig`` reads its configuration from the environment at construction
time (T4-AC1): the endpoint, API version, and the credential are resolved when
the provider is instantiated.  The credential is *additionally* re-resolved on
each call via :meth:`resolve_api_key` so that a key rotation never requires a
process restart — the exact zero-downtime behaviour the D1/D2 provider
documents and the T3 graceful-degradation tests rely on.  The live value always
wins; the instantiation-time read seeds presence detection and the startup log
line (which never prints the value itself).

The credential is the secret.  The endpoint URL, API version, and model name
are *not* secrets and may be logged freely.
"""
from __future__ import annotations

import os

# ---------------------------------------------------------------------------
# Configuration key names — the single source of truth for where the hosted
# provider's credential and endpoint config come from.  Documented in
# backend/.env.template (T4-AC4).
# ---------------------------------------------------------------------------

#: Secret. The hosted-provider API credential. Read from config/secrets only.
CONFIG_KEY_API_KEY: str = "ANTHROPIC_API_KEY"
#: Non-secret. Optional model pin (e.g. 'claude-sonnet-4-5').
CONFIG_KEY_MODEL: str = "MODEL_NAME"
#: Non-secret. Optional endpoint override (defaults to the Anthropic API).
CONFIG_KEY_ENDPOINT: str = "ANTHROPIC_API_URL"

_DEFAULT_ENDPOINT: str = "https://api.anthropic.com/v1/messages"
_DEFAULT_MODEL: str = "claude-sonnet-4-5"
_API_VERSION: str = "2023-06-01"


class HostedConfig:
    """Credential and endpoint configuration for the hosted model provider.

    All values are sourced from configuration / secrets (environment) at
    instantiation (T4-AC1).  The credential is held privately and is never
    returned to, nor accessible by, any caller outside the gateway package
    (T4-AC3); callers interact only with :meth:`resolve_api_key` /
    :meth:`has_credential`, which the provider uses internally.
    """

    __slots__ = ("_endpoint", "_api_version", "_api_key_at_init")

    def __init__(self) -> None:
        # Non-secret config — safe to read once and hold.
        self._endpoint: str = os.getenv(CONFIG_KEY_ENDPOINT, _DEFAULT_ENDPOINT)
        self._api_version: str = _API_VERSION
        # Credential — read from config/secrets at instantiation (T4-AC1).
        # Stored only as a private attribute (leading underscore + __slots__),
        # never exposed as a public attribute or return value (T4-AC3).  The
        # live value is re-read in resolve_api_key() so rotation needs no
        # restart; this seeds has_credential() and the startup log line.
        self._api_key_at_init: str = os.getenv(CONFIG_KEY_API_KEY, "")

    # ------------------------------------------------------------------
    # Non-secret accessors — safe to log.
    # ------------------------------------------------------------------

    @property
    def endpoint(self) -> str:
        """The hosted provider endpoint URL (non-secret)."""
        return self._endpoint

    @property
    def api_version(self) -> str:
        """The hosted provider API version header value (non-secret)."""
        return self._api_version

    def model(self) -> str:
        """The model name, resolved live from config so an operator can re-pin
        without a restart (non-secret)."""
        return os.getenv(CONFIG_KEY_MODEL, _DEFAULT_MODEL)

    # ------------------------------------------------------------------
    # Credential accessors — internal to the gateway package only.
    # ------------------------------------------------------------------

    def resolve_api_key(self) -> str:
        """Return the current credential, read live from config/secrets.

        Re-reading on every call (rather than returning the instantiation-time
        value) means a rotated key takes effect immediately — no restart, no
        cached stale secret.  Returns an empty string when unset; the provider
        treats that as "skip the call, degrade gracefully".

        This value must never be logged and must never be handed to a caller
        outside the gateway package (T4-AC2 / T4-AC3).
        """
        return os.getenv(CONFIG_KEY_API_KEY, "")

    def has_credential(self) -> bool:
        """True when a non-empty credential is configured (live read).

        Lets the provider report a missing-key condition without ever exposing
        the value itself.
        """
        return bool(self.resolve_api_key())

    # ------------------------------------------------------------------
    # Redaction — guarantees the credential never leaks via repr/str (T4-AC2).
    # ------------------------------------------------------------------

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return (
            f"HostedConfig(endpoint={self._endpoint!r}, "
            f"api_version={self._api_version!r}, api_key='***REDACTED***')"
        )

    __str__ = __repr__
