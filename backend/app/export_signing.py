"""
export_signing.py — 2.0-D4 T2: the ONE signing scheme for customer-facing exports.

An export a customer hands to an auditor has to answer a question the platform
cannot answer for itself: *is this file the file the system produced?* This module
is the answer, and it is deliberately a SHARED module rather than a helper inside
the audit export, because 2.0-B1's evidence-bundle export needs the identical
guarantee. A platform with two signature schemes has a weakest one, and reviewers
find it.

Why Ed25519, and why not an HMAC
--------------------------------
Reused rather than re-chosen: ``app/licensing.py`` already verifies licence keys
with Ed25519 via ``cryptography``, including a ``load_trusted_key_set()`` that
supports several trusted keys at once. Building on the same asymmetric scheme gives
the customer's auditor something they can verify INDEPENDENTLY from a published
public key.

That independence is the whole point, and it is what an HMAC cannot provide: with a
shared secret, any signature the customer can verify is also a signature the vendor
could have produced, so "CloudFulcrum says this file is authentic" is the strongest
claim available. With Ed25519 the private half never leaves the deployment, so the
auditor is verifying the DEPLOYMENT's attestation, not the vendor's.

Key separation — a deliberate decision (2.0-D4 T2)
--------------------------------------------------
Audit exports are signed by a **different key** from licences. The two are
different capabilities held by different parties:

  * Licence signing is a VENDOR-side capability. The private key lives in
    CloudFulcrum's AWS Secrets Manager and is used by the issuing service; a
    customer must never be able to mint a licence.
  * Audit-export signing is a DEPLOYMENT-side capability. The customer's own
    installation attests that this export is what it produced, so the private key
    must live in the customer's deployment — which is precisely where the licence
    key must NOT be.

Sharing one key would therefore either put licence-minting power inside every
customer deployment, or make every audit export a vendor attestation the customer
cannot produce alone. Both are worse than managing two keys.

Configuration
-------------
``AUDIT_EXPORT_SIGNING_KEY``  — the deployment's PRIVATE key (PEM, PKCS#8).
``AUDIT_EXPORT_PUBLIC_KEY``   — the matching public key (PEM), published to the
                                auditor and used by :func:`verify_export`.

Both are resolved live per call, so a key rotation is a config change. There is no
baked-in default and no fallback: an unconfigured deployment cannot sign, and
:func:`sign_export` raises rather than emitting an unsigned artifact that looks
signed. A silent downgrade to "no signature" is the one failure mode a compliance
export must not have.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
from typing import Any, Dict, Optional, Tuple

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

logger = logging.getLogger(__name__)

#: Env var holding the deployment's PRIVATE export-signing key (PEM, PKCS#8).
SIGNING_KEY_ENV = "AUDIT_EXPORT_SIGNING_KEY"
#: Env var holding the matching PUBLIC key (PEM) an auditor verifies against.
PUBLIC_KEY_ENV = "AUDIT_EXPORT_PUBLIC_KEY"

#: Signature-envelope version. Bump when the signed-bytes construction changes, so
#: a signature made under one rule can never silently verify under another.
SIGNATURE_VERSION = "1"

#: The algorithm name recorded in the envelope. Recorded rather than assumed so an
#: auditor's verifier knows what to use without reading our source.
SIGNATURE_ALGORITHM = "Ed25519"


class ExportSigningError(RuntimeError):
    """Raised when an export cannot be signed (typically: no key configured).

    Deliberately fatal. An export that silently loses its signature is worse than
    a failed export: the customer hands the auditor a file that looks like
    evidence and is not.
    """


# ── key loading ────────────────────────────────────────────────────────────────


def load_signing_key(pem: Optional[str] = None) -> Ed25519PrivateKey:
    """Load the deployment's private export-signing key.

    ``pem`` is for tests, which use a throwaway key; production resolves
    :data:`SIGNING_KEY_ENV` live.
    """
    raw = pem or os.getenv(SIGNING_KEY_ENV) or ""
    if not raw.strip():
        raise ExportSigningError(
            f"{SIGNING_KEY_ENV} is not configured — this deployment cannot sign "
            f"audit exports. Generate an Ed25519 key pair and set "
            f"{SIGNING_KEY_ENV} (private) and {PUBLIC_KEY_ENV} (public); see "
            f"docs/audit_export_and_retention.md."
        )
    try:
        key = serialization.load_pem_private_key(raw.encode(), password=None)
    except Exception as exc:  # noqa: BLE001 — surface a clear cause, never the key
        raise ExportSigningError(
            f"{SIGNING_KEY_ENV} is not a readable PEM private key "
            f"({type(exc).__name__})"
        ) from None
    if not isinstance(key, Ed25519PrivateKey):
        raise ExportSigningError(f"{SIGNING_KEY_ENV} must be an Ed25519 private key")
    return key


def load_verification_key(pem: Optional[str] = None) -> Ed25519PublicKey:
    """Load the public key an export is verified against."""
    raw = pem or os.getenv(PUBLIC_KEY_ENV) or ""
    if not raw.strip():
        raise ExportSigningError(
            f"{PUBLIC_KEY_ENV} is not configured — cannot verify an audit export."
        )
    key = serialization.load_pem_public_key(raw.encode())
    if not isinstance(key, Ed25519PublicKey):
        raise ExportSigningError(f"{PUBLIC_KEY_ENV} must be an Ed25519 public key")
    return key


def public_key_pem(private_key: Ed25519PrivateKey) -> str:
    """The PEM public half of a private key — what an auditor is given."""
    return (
        private_key.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
        .strip()
    )


def generate_key_pair() -> Tuple[str, str]:
    """Generate a fresh (private PEM, public PEM) pair — provisioning aid."""
    private = Ed25519PrivateKey.generate()
    private_pem = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    return private_pem, public_key_pem(private)


# ── canonical bytes ────────────────────────────────────────────────────────────


def canonical_bytes(payload: Dict[str, Any]) -> bytes:
    """The exact bytes that are signed and verified.

    Deterministic by construction — sorted keys, no insertion-order dependence,
    fixed separators, UTF-8 — because the signature is over BYTES and any
    non-determinism in serialisation would make a genuine export fail its own
    verification. ``ensure_ascii=False`` keeps customer names readable rather than
    escaped; the encoding is pinned so that choice cannot change the bytes.
    """
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def content_digest(payload_bytes: bytes) -> str:
    """SHA-256 of the signed bytes, recorded in the envelope.

    Not a substitute for the signature — a digest proves nothing on its own, since
    anyone altering the content can recompute it. It is there so a reader can tell
    WHICH part failed: a digest mismatch means the file was truncated or re-encoded
    in transit, a signature mismatch with a matching digest means the content was
    deliberately rewritten.
    """
    return hashlib.sha256(payload_bytes).hexdigest()


# ── sign / verify ──────────────────────────────────────────────────────────────


def sign_export(
    payload: Dict[str, Any],
    *,
    private_key: Optional[Ed25519PrivateKey] = None,
    key_id: str = "deployment",
) -> Dict[str, Any]:
    """Wrap ``payload`` in a signed envelope.

    The returned document is what the customer hands over: the payload verbatim
    plus a ``signature`` block carrying the algorithm, the version of the
    signed-bytes rule, the key id, the content digest, and the signature itself.

    The signature covers :func:`canonical_bytes` of the PAYLOAD ONLY — never the
    envelope — so verification can reconstruct exactly what was signed without
    having to strip its own signature back out, a step that is easy to get subtly
    wrong and impossible to notice when it is wrong.
    """
    key = private_key or load_signing_key()
    body = canonical_bytes(payload)
    signature = key.sign(body)
    return {
        **payload,
        "signature": {
            "algorithm": SIGNATURE_ALGORITHM,
            "version": SIGNATURE_VERSION,
            "key_id": key_id,
            "content_sha256": content_digest(body),
            "value": base64.b64encode(signature).decode(),
        },
    }


def verify_export(
    document: Dict[str, Any],
    *,
    public_key: Optional[Ed25519PublicKey] = None,
) -> Tuple[bool, str]:
    """Verify a signed export. Returns ``(ok, reason)``.

    Never raises for a bad document — a verifier that throws on malformed input is
    hard to use as a check, and "this file did not verify, because X" is the answer
    the auditor needs. ``reason`` is ``"verified"`` on success and names the failure
    otherwise.
    """
    if not isinstance(document, dict):
        return False, "document is not an object"
    envelope = document.get("signature")
    if not isinstance(envelope, dict):
        return False, "no signature block"

    if envelope.get("algorithm") != SIGNATURE_ALGORITHM:
        return False, f"unexpected algorithm {envelope.get('algorithm')!r}"
    if str(envelope.get("version")) != SIGNATURE_VERSION:
        # A signature made under a different signed-bytes rule must never be
        # verified under this one, even if the maths happens to work out.
        return False, f"unsupported signature version {envelope.get('version')!r}"

    payload = {k: v for k, v in document.items() if k != "signature"}
    body = canonical_bytes(payload)

    expected_digest = envelope.get("content_sha256")
    actual_digest = content_digest(body)
    if expected_digest and expected_digest != actual_digest:
        return False, "content digest mismatch — the export was altered"

    try:
        raw_signature = base64.b64decode(str(envelope.get("value") or ""), validate=True)
    except Exception:  # noqa: BLE001
        return False, "signature is not valid base64"
    if not raw_signature:
        return False, "signature is empty"

    try:
        key = public_key or load_verification_key()
    except ExportSigningError as exc:
        return False, str(exc)

    try:
        key.verify(raw_signature, body)
    except InvalidSignature:
        return False, "signature does not match the content — the export was altered"
    except Exception as exc:  # noqa: BLE001
        return False, f"verification failed ({type(exc).__name__})"
    return True, "verified"
