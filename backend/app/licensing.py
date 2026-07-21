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
import os
from typing import Optional

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import load_pem_public_key

# ===========================================================================
# T2 (AT-343): CloudFulcrum public key — the root of trust.
# Safe to ship; published in the binary by design. The matching private key is
# held only by the CloudFulcrum issuing service / secrets manager.
# Rotation: prefer the LICENSE_PUBLIC_KEY env override (see load_public_key) so a
# key rotation needs only a config change, not a code change + release. If the
# private key is compromised, rotate the env value (or, as a last resort, replace
# the constant below and cut a release). See backend/license/README.md →
# "Key rotation runbook".
# ---------------------------------------------------------------------------
CLOUDFULCRUM_PUBLIC_KEY = """-----BEGIN PUBLIC KEY-----
MCowBQYDK2VwAyEA6TBkcZABXy0U9JQ8x1TLBmcqFvGbAwxA/juJIdbyNpI=
-----END PUBLIC KEY-----"""

# Optional env override for the trusted public key (PEM). Lets an operator rotate
# the root of trust without a release cycle — same pattern as JWT secrets. Falls
# back to the baked-in constant when unset.
LICENSE_PUBLIC_KEY_ENV = "LICENSE_PUBLIC_KEY"


def load_public_key(pem: Optional[str] = None) -> Ed25519PublicKey:
    """Load the Ed25519 public key from PEM text.

    Resolution order:
      1. an explicit ``pem`` argument (used by tests with a throwaway key),
      2. the ``LICENSE_PUBLIC_KEY`` env var (rotation without a release),
      3. the baked-in ``CLOUDFULCRUM_PUBLIC_KEY`` constant.
    """
    pem = pem or os.getenv(LICENSE_PUBLIC_KEY_ENV) or CLOUDFULCRUM_PUBLIC_KEY
    key = load_pem_public_key(pem.encode())
    if not isinstance(key, Ed25519PublicKey):
        raise TypeError("configured license public key is not an Ed25519 public key")
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


# Structured invalid-reason codes. Stable machine-readable strings the status
# API / banner map to plain-language copy; keep them in sync with the UI reason
# map. ``signature_or_format`` is the catch-all for any signature/format/parse
# failure (AC2); ``org_mismatch`` (R-1.9.1-L1 / T2) is a signature-valid key
# bound to a DIFFERENT installation org.
REASON_SIGNATURE_OR_FORMAT = "signature_or_format"
REASON_ORG_MISMATCH = "org_mismatch"

# Exact failure shape returned on any signature/format/parse error (AC2).
_INVALID_RESULT = {"status": LicenseStatus.INVALID, "reason": REASON_SIGNATURE_OR_FORMAT}

DEFAULT_GRACE_DAYS = 14


def _invalid(reason: str) -> dict:
    """A fresh ``invalid`` result carrying the given machine-readable reason."""
    return {"status": LicenseStatus.INVALID, "reason": reason}


def validate_license(
    key_string: str,
    public_key: Optional[Ed25519PublicKey] = None,
    *,
    installation_org_id: Optional[str] = None,
) -> dict:
    """Validate a license key fully offline and return a status dict.

    Never raises — any malformed input, bad signature (AC2), or unparseable
    payload returns ``{'status': 'invalid', 'reason': 'signature_or_format'}``.

    On a verified key, status is derived from the system clock:
      * ``today <= expires_at``                       -> ``valid``
      * ``expires_at < today <= expires_at + grace``  -> ``grace``
      * ``today > expires_at + grace``                -> ``readonly``

    Org binding (R-1.9.1-L1 / T2, AC1): when ``installation_org_id`` is supplied
    and the verified payload carries an ``org_id`` (payload v2), the two must
    match. A signature-valid key whose ``org_id`` is bound to a DIFFERENT
    installation org returns ``{'status': 'invalid', 'reason': 'org_mismatch'}``
    — the "Customer A's key pasted into Customer B's install must fail closed and
    say why" case. This check runs BEFORE the date logic: an org-mismatched key
    is invalid regardless of its term. The comparison is skipped (no binding
    enforced) when the caller passes no ``installation_org_id`` — so the pure,
    org-agnostic callers (e.g. offline issuing tests) are unaffected — and also
    when the payload has no ``org_id`` (a pre-v2 key), which is handled by the
    separate v1-rejection step (T4), not here.

    Returns ``{status, customer, deployment_type, expires_at, days_remaining,
    payload}`` for a verified key. ``deployment_type`` (R-1.9.1-L1 / T1, payload
    v2) is lifted to the top level so the status API can expose it without every
    caller reaching into ``payload`` (AC5); it is ``None`` for a pre-v2 key that
    carries no such field. ``public_key`` defaults to the baked-in CloudFulcrum
    key; tests pass a throwaway key to exercise the date logic without the real
    private key.
    """
    payload = verify_license_signature(key_string, public_key)
    if payload is None:
        return dict(_INVALID_RESULT)

    # Org binding (T2 / AC1) — checked before the date logic so a key bound to a
    # different org is invalid regardless of its term. Only enforced when the
    # caller names an installation org AND the payload declares an org_id; a
    # pre-v2 key with no org_id is left to the T4 v1-rejection path.
    payload_org_id = payload.get("org_id")
    if (
        installation_org_id is not None
        and payload_org_id is not None
        and payload_org_id != installation_org_id
    ):
        return _invalid(REASON_ORG_MISMATCH)

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
        "deployment_type": payload.get("deployment_type"),
        "expires_at": payload["expires_at"],
        "days_remaining": (expires - today).days,
        "payload": payload,
    }
