"""LIC-1 Offline License Key System — in-app licensing primitives.

This module is the *shipped* side of the LIC-1 scheme. It is safe to bake into
the customer build: it contains only the CloudFulcrum **public** key and the
offline signature-verification primitive. The private signing key lives only on
the CloudFulcrum issuing service and is never present here.

Boundary between tickets (do not blur):
  * AT-343 (T2) — owns the block below: ``CLOUDFULCRUM_PUBLIC_KEY``,
    ``load_public_key()`` and the ``verify_license_signature()`` primitive.
  * AT-344 (T3) — will add ``LicenseStatus`` and ``validate_license()`` (the
    expiry/grace/read-only status logic) *below* the marked T3 section, calling
    ``verify_license_signature()`` rather than re-implementing verification.

Verification is fully offline (AC1): no network call to CloudFulcrum is ever
made from this module.
"""

from __future__ import annotations

import base64
import datetime
import json
from typing import Optional

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import load_pem_public_key

# ===========================================================================
# T2 (AT-343): CloudFulcrum public key — the root of trust.
# Safe to ship; published in the binary by design. The matching private key is
# held only by the CloudFulcrum issuing service / secrets manager.
# Rotation: if the private key is ever compromised, replace the constant below
# and cut a release. See license/README.md → "Key rotation runbook".
# ---------------------------------------------------------------------------
CLOUDFULCRUM_PUBLIC_KEY = """-----BEGIN PUBLIC KEY-----
MCowBQYDK2VwAyEA6TBkcZABXy0U9JQ8x1TLBmcqFvGbAwxA/juJIdbyNpI=
-----END PUBLIC KEY-----"""


def load_public_key(pem: str = CLOUDFULCRUM_PUBLIC_KEY) -> Ed25519PublicKey:
    """Load the baked-in (or a supplied) Ed25519 public key from PEM text."""
    key = load_pem_public_key(pem.encode())
    if not isinstance(key, Ed25519PublicKey):
        raise TypeError("CLOUDFULCRUM_PUBLIC_KEY is not an Ed25519 public key")
    return key


def verify_license_signature(
    key_string: str,
    public_key: Optional[Ed25519PublicKey] = None,
) -> Optional[dict]:
    """Verify a license key's signature **offline** and return its payload.

    The key string is ``base64(payload).base64(signature)`` where the signature
    is over the ``base64(payload)`` bytes (see the issuing scheme).

    Returns the decoded payload dict if the signature is valid, otherwise
    ``None``. Never raises — a malformed string, bad base64, wrong key, or a
    tampered payload (AC2) all return ``None``.

    ``public_key`` defaults to the baked-in CloudFulcrum key; tests pass a
    throwaway key to exercise the contract without the real private key.
    """
    if public_key is None:
        public_key = load_public_key()
    try:
        payload_b64, sig_b64 = key_string.split(".")
        public_key.verify(base64.b64decode(sig_b64), payload_b64.encode())
        return json.loads(base64.b64decode(payload_b64))
    except Exception:
        return None


# ===========================================================================
# T3 (AT-344): offline validation core — LicenseStatus + validate_license().
# Built on top of verify_license_signature() above (no duplicate verification).
# Pure and side-effect-free: no DB, no telemetry, no main.py wiring (that is T4).
# ===========================================================================
class LicenseStatus:
    """License states. Mirrored by the admin UI badge and the run gate."""

    VALID = "valid"        # within term — full function
    GRACE = "grace"        # past expiry, within grace_days — warn, still full function
    READONLY = "readonly"  # past grace — discovery blocked, findings viewable
    INVALID = "invalid"    # signature failed, no key, or unparseable payload


# Exact failure shape returned on any signature/format/parse error (AC2).
_INVALID_RESULT = {"status": LicenseStatus.INVALID, "reason": "signature_or_format"}

DEFAULT_GRACE_DAYS = 14


def validate_license(
    key_string: str,
    public_key: Optional[Ed25519PublicKey] = None,
) -> dict:
    """Validate a license key fully offline and return a status dict.

    Never raises — any malformed input, bad signature (AC2), or unparseable
    payload returns ``{'status': 'invalid', 'reason': 'signature_or_format'}``.

    On a verified key, status is derived from the system clock:
      * ``today <= expires_at``                       -> ``valid``
      * ``expires_at < today <= expires_at + grace``  -> ``grace``
      * ``today > expires_at + grace``                -> ``readonly``

    Returns ``{status, customer, expires_at, days_remaining, payload}`` for a
    verified key. ``public_key`` defaults to the baked-in CloudFulcrum key;
    tests pass a throwaway key to exercise the date logic without the real
    private key.
    """
    payload = verify_license_signature(key_string, public_key)
    if payload is None:
        return dict(_INVALID_RESULT)

    try:
        today = datetime.date.today()
        expires = datetime.date.fromisoformat(payload["expires_at"])
        grace_days = int(payload.get("grace_days", DEFAULT_GRACE_DAYS))
        grace_end = expires + datetime.timedelta(days=grace_days)
    except Exception:
        # Signature verified but the payload is structurally bad — still invalid.
        return dict(_INVALID_RESULT)

    if today <= expires:
        status = LicenseStatus.VALID
    elif today <= grace_end:
        status = LicenseStatus.GRACE
    else:
        status = LicenseStatus.READONLY

    return {
        "status": status,
        "customer": payload.get("customer"),
        "expires_at": payload["expires_at"],
        "days_remaining": (expires - today).days,
        "payload": payload,
    }
