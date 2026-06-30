"""OAuth state encoding & verification — R17-D3 / AT-447 (T2).

The OAuth ``state`` parameter leaves AgentIQ for the provider and returns on an
unauthenticated browser redirect. To stop a callback from being bound to the
wrong tenant, the AUTHENTICATED organisation that initiated the flow is carried
inside ``state`` and verified on return.

State format — three dot-separated segments::

    <org_id>.<nonce>.<signature>

  * org_id    — the initiating organisation (T2-AC1). Plain text so the value is
                inspectable; it is a non-secret internal id (a UUID, or the dev
                ``default`` org), never PII.
  * nonce     — the single-use, server-side state nonce (``secrets.token_urlsafe``).
                It is ALSO the key under which the server stores the flow's
                connector_id / PKCE verifier / org, so the callback can look it up.
  * signature — ``HMAC-SHA256(server_secret, "<org_id>.<nonce>")`` as hex. This
                makes the org_id tamper-evident: the browser/provider cannot alter
                the org without invalidating the signature (T2-AC2/AC3).

The signing key is a DEDICATED ``OAUTH_STATE_SECRET`` when set, falling back to
the server's JWT signing secret otherwise (see :func:`_state_secret`). Keeping the
two keys separable matters: OAuth-state signing and user-session signing are
different security domains, so rotating ``JWT_SECRET`` (e.g. after a suspected
session-token compromise) must not also invalidate every in-flight OAuth ``state``
(R17-D3 review H1). Verification uses :func:`hmac.compare_digest` (never ``==``) so
state validation is timing-safe, matching the nonce-comparison policy elsewhere in
this package.

Disclosure trade-off (R17-D3 review L1): ``org_id`` travels in the state as PLAIN
TEXT, so it is visible to the OAuth provider and to any HTTP intermediary that sees
the redirect URL. In production it is an opaque internal UUID (never PII), and the
HMAC makes it tamper-evident, so this is a deliberate, low-risk choice favouring
inspectability over opacity — documented here so a future maintainer does not
mistake it for an oversight.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)


def _state_secret() -> bytes:
    """Return the HMAC key used to sign/verify the OAuth ``state``.

    Prefers a DEDICATED ``OAUTH_STATE_SECRET`` so the OAuth-state signing key and
    the user-session JWT signing key stay in separate security domains: rotating
    ``JWT_SECRET`` must not also invalidate every in-flight OAuth ``state`` (H1).
    Falls back to the JWT signing secret when ``OAUTH_STATE_SECRET`` is unset, so
    existing deployments keep working until the dedicated key is provisioned.

    Resolved at call time (never cached at import) so a rotated secret takes effect
    without a process restart, and so importing this module never forces secret
    resolution at startup.
    """
    dedicated = os.environ.get("OAUTH_STATE_SECRET")
    if dedicated:
        return dedicated.encode("utf-8")
    from app.auth.user_auth import _jwt_secret

    return _jwt_secret().encode("utf-8")


def _sign(payload: str) -> str:
    return hmac.new(_state_secret(), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def encode_state(org_id: str, nonce: str) -> str:
    """Return a signed ``state`` value carrying ``org_id`` and ``nonce`` (T2-AC1).

    Raises ``ValueError`` if ``org_id`` or ``nonce`` is empty — an unbound state
    (one that could not later be tied back to a tenant) must never be produced.
    """
    if not org_id or not nonce:
        raise ValueError("encode_state requires a non-empty org_id and nonce")
    payload = f"{org_id}.{nonce}"
    return f"{payload}.{_sign(payload)}"


def decode_state(state: Optional[str]) -> Optional[dict]:
    """Verify ``state`` and return ``{"org_id", "nonce"}``, or ``None`` if invalid.

    Returns ``None`` for a missing, malformed, or signature-mismatched state; the
    caller maps that to a generic HTTP 400 and must never reveal which check
    failed. The HMAC is compared with :func:`hmac.compare_digest`, so a forged or
    tampered org_id is rejected without leaking timing information (T2-AC2/AC3).
    """
    if not state:
        return None
    # rsplit so an org_id is recovered intact even in the unlikely case it
    # contains a '.'; the nonce (token_urlsafe) and the hex signature never do.
    parts = state.rsplit(".", 2)
    if len(parts) != 3:
        return None
    org_id, nonce, signature = parts
    if not org_id or not nonce or not signature:
        return None
    expected = _sign(f"{org_id}.{nonce}")
    if not hmac.compare_digest(signature, expected):
        return None
    return {"org_id": org_id, "nonce": nonce}
