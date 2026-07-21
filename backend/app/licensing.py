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
import logging
import os
from typing import Dict, Optional

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import load_pem_public_key

logger = logging.getLogger(__name__)

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

# R-1.9.1-L1 / T3 (AT-689): the trusted public keys are a KEYED SET, not a single
# constant. A payload v2 license carries a ``kid`` (key identifier); verification
# selects the trusted public key by that kid. This makes signing-key rotation a
# CONFIG change (add the new kid's public key here, issue under the new kid, retire
# the old kid once no key references it) rather than a binary release.
#
# ``DEFAULT_KID`` is the kid the baked-in / LICENSE_PUBLIC_KEY-overridden root of
# trust is registered under — it MUST match the issuer's default kid in
# ``backend/license/generate_license.py`` so a key issued with the default kid
# verifies against the shipped root of trust.
DEFAULT_KID = "cf-2026-1"

# Optional env override carrying the WHOLE trusted key set as JSON: an object
# mapping ``kid`` -> PEM public key, e.g.
#   {"cf-2026-1": "-----BEGIN PUBLIC KEY-----\n...", "cf-2027-2": "..."}
# Merged over the baked-in default (so the default kid is always present unless
# explicitly overridden). Lets an operator add/rotate signing keys without a
# release. Unset -> the set is just {DEFAULT_KID: <load_public_key()>}.
LICENSE_TRUSTED_KEYS_ENV = "LICENSE_TRUSTED_KEYS"


def load_public_key(pem: Optional[str] = None) -> Ed25519PublicKey:
    """Load the Ed25519 public key from PEM text.

    Resolution order:
      1. an explicit ``pem`` argument (used by tests with a throwaway key),
      2. the ``LICENSE_PUBLIC_KEY`` env var (rotation without a release),
      3. the baked-in ``CLOUDFULCRUM_PUBLIC_KEY`` constant.

    This remains the single-key resolver (the root of trust registered under
    ``DEFAULT_KID``). The keyed set is built on top of it in
    :func:`load_trusted_key_set`.
    """
    pem = pem or os.getenv(LICENSE_PUBLIC_KEY_ENV) or CLOUDFULCRUM_PUBLIC_KEY
    key = load_pem_public_key(pem.encode())
    if not isinstance(key, Ed25519PublicKey):
        raise TypeError("configured license public key is not an Ed25519 public key")
    return key


def load_trusted_key_set() -> Dict[str, Ed25519PublicKey]:
    """Build the ``kid`` -> trusted public key set (R-1.9.1-L1 / T3).

    The set always contains the root of trust under ``DEFAULT_KID`` (from
    ``load_public_key`` — i.e. the ``LICENSE_PUBLIC_KEY`` env override or the
    baked-in constant). When ``LICENSE_TRUSTED_KEYS`` is set to a JSON object of
    ``{kid: pem}``, each entry is added; an entry for ``DEFAULT_KID`` there
    overrides the baked-in default. This is how a second/rotated signing key is
    trusted without a release.

    Resolved live per call (like ``load_public_key``) so a config change takes
    effect without a restart. Never raises: a malformed ``LICENSE_TRUSTED_KEYS``
    value, or an individual entry that is not a valid Ed25519 public key, is
    logged and skipped — the set degrades to whatever entries parsed cleanly,
    always including the baked-in default so verification never loses its root of
    trust from bad config.
    """
    key_set: Dict[str, Ed25519PublicKey] = {}

    # Always start from the single-key root of trust under the default kid.
    try:
        key_set[DEFAULT_KID] = load_public_key()
    except Exception:  # pragma: no cover — a broken baked-in/env key is catastrophic
        logger.exception("license: could not load the default trusted public key")

    raw = os.getenv(LICENSE_TRUSTED_KEYS_ENV)
    if raw:
        try:
            entries = json.loads(raw)
            if not isinstance(entries, dict):
                raise ValueError("LICENSE_TRUSTED_KEYS must be a JSON object of {kid: pem}")
        except Exception:
            logger.exception(
                "license: LICENSE_TRUSTED_KEYS is not valid JSON — ignoring the override"
            )
            entries = {}
        for kid, pem in entries.items():
            if not isinstance(kid, str) or not isinstance(pem, str):
                logger.warning("license: skipping non-string trusted-key entry %r", kid)
                continue
            try:
                key = load_pem_public_key(pem.encode())
                if not isinstance(key, Ed25519PublicKey):
                    raise TypeError("not an Ed25519 public key")
                key_set[kid] = key
            except Exception:
                logger.warning("license: trusted key for kid %r is invalid — skipping", kid)

    return key_set


# Outcome sentinel for the internal verifier: signature-valid but the payload's
# kid is not in the trusted key set (R-1.9.1-L1 / T3 — surfaced as unknown_key).
_UNKNOWN_KID = object()


def _decode_payload_unverified(payload_b64: str) -> Optional[dict]:
    """Decode the base64(json) payload WITHOUT verifying — for kid selection only.

    The result is untrusted until the signature is checked against the selected
    key; it is used solely to read ``kid`` so the right trusted key can be picked
    (the standard JWS ``kid`` pattern). Returns ``None`` on any decode error.
    """
    try:
        decoded = json.loads(base64.b64decode(payload_b64))
        return decoded if isinstance(decoded, dict) else None
    except Exception:
        return None


def _verify_and_decode(key_string: str, public_key: Optional[Ed25519PublicKey]):
    """Internal verifier returning a structured outcome.

    Returns one of:
      * a payload ``dict`` — signature verified,
      * ``_UNKNOWN_KID``   — the payload names a ``kid`` not in the trusted set,
      * ``None``           — any other failure (bad format, wrong key, tamper).

    Key selection (R-1.9.1-L1 / T3):
      * If ``public_key`` is given explicitly, it is used directly — the test /
        single-key path, kid ignored.
      * Otherwise the payload's ``kid`` (if present) selects the trusted key from
        ``load_trusted_key_set()``; an unknown kid yields ``_UNKNOWN_KID``.
      * A payload with NO ``kid`` (pre-v2 / single-key mode) falls back to
        ``load_public_key()`` — preserving the LICENSE_PUBLIC_KEY env-rotation and
        baked-in behaviour, and the monkeypatched-``load_public_key`` test path.
    """
    parts = key_string.split(".")
    if len(parts) != 2:
        return None
    payload_b64, sig_b64 = parts

    # Resolve which public key to verify against.
    if public_key is None:
        header = _decode_payload_unverified(payload_b64)
        kid = header.get("kid") if header else None
        if kid is not None:
            key_set = load_trusted_key_set()
            selected = key_set.get(kid)
            if selected is None:
                return _UNKNOWN_KID  # AC2: signed-but-unknown kid
            public_key = selected
        else:
            # No kid → single-key mode (pre-v2, env override, or baked-in).
            public_key = load_public_key()

    try:
        public_key.verify(base64.b64decode(sig_b64), payload_b64.encode())
        return json.loads(base64.b64decode(payload_b64))
    except Exception:
        return None


def verify_license_signature(
    key_string: str,
    public_key: Optional[Ed25519PublicKey] = None,
) -> Optional[dict]:
    """Verify a license key's signature **offline** and return its payload.

    The key string is ``base64(payload).base64(signature)`` where the signature
    is over the ``base64(payload)`` bytes (see the issuing scheme).

    Returns the decoded payload dict if the signature is valid, otherwise
    ``None``. Never raises — a malformed string, bad base64, wrong key, an unknown
    ``kid``, or a tampered payload (AC2) all return ``None``. Callers that need to
    distinguish an unknown-kid failure from a generic one use
    :func:`validate_license` (which reports ``reason='unknown_key'``); this
    primitive collapses both to ``None`` to preserve its historical contract.

    When ``public_key`` is omitted the trusted key is selected by the payload's
    ``kid`` from the configured key set (R-1.9.1-L1 / T3), falling back to the
    single baked-in / ``LICENSE_PUBLIC_KEY`` key for a kid-less payload.
    ``public_key`` defaults to the baked-in CloudFulcrum key; tests pass a
    throwaway key to exercise the contract without the real private key.
    """
    result = _verify_and_decode(key_string, public_key)
    if result is _UNKNOWN_KID or result is None:
        return None
    return result


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
# bound to a DIFFERENT installation org; ``unknown_key`` (R-1.9.1-L1 / T3) is a
# key whose ``kid`` is not in the configured trusted key set.
REASON_SIGNATURE_OR_FORMAT = "signature_or_format"
REASON_ORG_MISMATCH = "org_mismatch"
REASON_UNKNOWN_KEY = "unknown_key"

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

    Key-set / kid verification (R-1.9.1-L1 / T3, AC2): when ``public_key`` is not
    passed explicitly, the trusted public key is selected by the payload's ``kid``
    from the configured key set. A signature that is otherwise well-formed but
    carries a ``kid`` NOT in the trusted set returns
    ``{'status': 'invalid', 'reason': 'unknown_key'}`` — distinct from a generic
    ``signature_or_format`` failure — so an operator can tell "I need to trust /
    rotate this signing key" from "this key is corrupt or forged".

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
    verified = _verify_and_decode(key_string, public_key)
    if verified is _UNKNOWN_KID:
        # Signed, but by a kid we don't trust — a rotation/config signal, not a
        # forgery (AC2). Kept distinct from signature_or_format.
        return _invalid(REASON_UNKNOWN_KEY)
    if verified is None:
        return dict(_INVALID_RESULT)
    payload = verified

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
