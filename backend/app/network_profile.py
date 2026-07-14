"""R18-A3 T5 (AT-558) — NETWORK_PROFILE deployment flag.

A deployment declares its inbound-network posture with a single env var:

    NETWORK_PROFILE = 'standard' | 'no_public_inbound'

``standard`` (default)
    The deployment can accept inbound HTTPS, so the browser authorization-code
    OAuth flow (provider → redirect → callback) can complete. Nothing changes.

``no_public_inbound``
    The deployment exposes NO public inbound HTTPS (the default posture of
    security-conscious banks — TCU is the motivating case). A provider can never
    redirect back into the network, so the authorization-code callback can never
    arrive. In this profile the UI must NOT offer an authorization-code connect
    flow for any connector that has an outbound-only auth mode configured
    (client_credentials / jwt_bearer / static) — the customer would otherwise
    start a flow that cannot complete (AC4). It shows the outbound setup path
    instead.

This module is the single source of truth for reading the flag. It never leaks
into ingestion — like the auth-mode concept (R18-A3 T1) it lives at the
connect/setup edge only. The per-connector capability that the UI pairs with
this flag lives in :mod:`app.auth.auth_modes`
(:func:`get_connector_auth_capability`).
"""
from __future__ import annotations

import os
from typing import Literal

NetworkProfile = Literal["standard", "no_public_inbound"]

#: The two recognised deployment postures.
NETWORK_PROFILE_STANDARD: NetworkProfile = "standard"
NETWORK_PROFILE_NO_PUBLIC_INBOUND: NetworkProfile = "no_public_inbound"

_VALID_PROFILES: frozenset[str] = frozenset(
    {NETWORK_PROFILE_STANDARD, NETWORK_PROFILE_NO_PUBLIC_INBOUND}
)

#: Env var that carries the deployment profile.
_ENV_VAR = "NETWORK_PROFILE"


def get_network_profile() -> NetworkProfile:
    """Return the deployment's network profile from the environment.

    Read live (not cached) so a test or an operator can flip it without a code
    change; the value is trivial to resolve. An unset, blank, or unrecognised
    value degrades to the safe default ``standard`` — an unknown profile must
    never silently hide connect flows, so we fail toward the full (standard)
    experience rather than the restricted one.
    """
    raw = (os.environ.get(_ENV_VAR) or "").strip().lower()
    if raw in _VALID_PROFILES:
        return raw  # type: ignore[return-value]
    return NETWORK_PROFILE_STANDARD


def is_no_public_inbound() -> bool:
    """True when the deployment exposes no public inbound HTTPS (Approach A)."""
    return get_network_profile() == NETWORK_PROFILE_NO_PUBLIC_INBOUND
