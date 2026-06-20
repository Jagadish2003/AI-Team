#!/usr/bin/env python3
"""LIC-1 (AT-342 / T1) — CloudFulcrum-internal license issuing CLI.

Local-signing variant (matches the LIC-1 design §3): this tool loads the
CloudFulcrum **private** Ed25519 key, assembles the signed payload, and prints
the license key string the customer pastes into AgentIQ. No network call is made
and no online activation exists — issuing is fully offline.

This directory (``backend/license/``) is CloudFulcrum-internal and is excluded
from the customer build/Docker image (AC10): it sits inside the backend build
context but ``backend/.dockerignore`` excludes ``license/``, so ``COPY . .``
never copies it into the image.

Key custody (AC10 / threat model): the private key is the single root secret of
the whole scheme. It lives only on the secured CloudFulcrum signing host /
secrets manager and is git-ignored here (``*.pem``). Generate it with
``generate_keypair.py`` (T2). If it leaks, every issued key is forgeable — rotate
per backend/license/README.md.

Usage (run from the repo root):
  python backend/license/generate_license.py \
    --customer 'City National Bank' \
    --license-id cnb-2026-001 \
    --term-months 12 \
    --grace-days 14 \
    --private-key backend/license/cloudfulcrum_private.pem

The signed key string is the ONLY thing printed to stdout, so it can be piped or
copied cleanly; everything else goes to stderr.
"""

from __future__ import annotations

import argparse
import base64
import datetime
import json
import os
import sys

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import load_pem_private_key

ALLOWED_TERMS = (3, 6, 12)
DEFAULT_PRIVATE_KEY = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "cloudfulcrum_private.pem"
)


def build_payload(
    customer: str,
    license_id: str,
    term_months: int,
    grace_days: int = 14,
    *,
    today: datetime.date | None = None,
) -> dict:
    """Assemble the full signed payload.

    ``issued_at`` is today and ``expires_at`` is today + term_months*30 days, so
    the term boundary is baked into the signed payload (the customer cannot move
    it without breaking the signature). ``limits`` are reserved (null) in v1 —
    present from day one so seat/pack limits can be enforced later without
    changing the key format.
    """
    issued = today or datetime.date.today()
    expires = issued + datetime.timedelta(days=term_months * 30)
    return {
        "customer": customer,
        "license_id": license_id,
        "issued_at": issued.isoformat(),
        "expires_at": expires.isoformat(),
        "term_months": term_months,
        "grace_days": grace_days,
        "limits": {"max_workspaces": None, "enabled_packs": None},
    }


def encode_payload(payload: dict) -> str:
    """Canonical payload encoding: base64(json with sorted keys).

    Must match the verifier's expectation exactly (``app.licensing`` decodes this
    same string), so the canonical JSON is produced with ``sort_keys=True``.
    """
    return base64.b64encode(json.dumps(payload, sort_keys=True).encode()).decode()


def sign_payload(payload: dict, private_key: Ed25519PrivateKey) -> str:
    """Sign a payload and return the ``base64(payload).base64(signature)`` key.

    The signature is over the ``base64(payload)`` bytes — the exact bytes the
    app verifies in ``verify_license_signature``.
    """
    payload_b64 = encode_payload(payload)
    sig_b64 = base64.b64encode(private_key.sign(payload_b64.encode())).decode()
    return f"{payload_b64}.{sig_b64}"


def load_private_key(path: str) -> Ed25519PrivateKey:
    """Load the CloudFulcrum private signing key from a PEM file."""
    with open(path, "rb") as fh:
        key = load_pem_private_key(fh.read(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise RuntimeError(f"{path} is not an Ed25519 private key")
    return key


def generate(
    customer: str,
    license_id: str,
    term_months: int,
    private_key_pem_path: str,
    grace_days: int = 14,
) -> str:
    """End-to-end: build payload, load the private key, return the signed key."""
    payload = build_payload(customer, license_id, term_months, grace_days)
    private_key = load_private_key(private_key_pem_path)
    return sign_payload(payload, private_key)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Issue a signed AgentIQ license key with the CloudFulcrum private key."
    )
    parser.add_argument("--customer", required=True)
    parser.add_argument("--license-id", required=True)
    parser.add_argument("--term-months", type=int, required=True, choices=ALLOWED_TERMS)
    parser.add_argument("--grace-days", type=int, default=14)
    parser.add_argument(
        "--private-key",
        default=DEFAULT_PRIVATE_KEY,
        help="Path to the CloudFulcrum private key PEM (git-ignored / secrets manager).",
    )
    args = parser.parse_args(argv)

    if not os.path.isfile(args.private_key):
        print(
            f"ERROR: private key not found at {args.private_key}. "
            "Generate it with generate_keypair.py or pass --private-key.",
            file=sys.stderr,
        )
        return 1

    try:
        key = generate(
            args.customer,
            args.license_id,
            args.term_months,
            args.private_key,
            args.grace_days,
        )
    except (OSError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(key)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
