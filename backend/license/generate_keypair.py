#!/usr/bin/env python3
"""LIC-1 (AT-343 / T2) — CloudFulcrum Ed25519 keypair generation.

Run ONCE (or on rotation) on the secured CloudFulcrum signing host to mint the
root-of-trust keypair for the offline license scheme:

  * The PRIVATE key is the only genuinely sensitive secret in the system. It is
    written to ``--out`` (default ``backend/license/cloudfulcrum_private.pem``,
    which is git-ignored) and must be moved into CloudFulcrum's secrets manager.
    It is NEVER committed and NEVER shipped to a customer.
  * The PUBLIC key is printed to stdout. Paste it into
    ``CLOUDFULCRUM_PUBLIC_KEY`` in ``backend/app/licensing.py`` — it is safe to
    ship and is published in the binary by design.

``generate_license.py`` (T1) signs license payloads with the private key this
tool produces; the app verifies them offline against the matching public key.

Usage (run from the repo root):
  python backend/license/generate_keypair.py
  python backend/license/generate_keypair.py --out /secure/path/cloudfulcrum_private.pem

Rotation: see "Key rotation runbook" in backend/license/README.md.
"""

from __future__ import annotations

import argparse
import os
import sys

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

DEFAULT_OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cloudfulcrum_private.pem")


def generate_keypair() -> tuple[str, str]:
    """Return ``(private_pem, public_pem)`` for a fresh Ed25519 keypair."""
    private_key = Ed25519PrivateKey.generate()
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = (
        private_key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    return private_pem, public_pem


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Generate the CloudFulcrum Ed25519 license keypair.")
    parser.add_argument(
        "--out",
        default=DEFAULT_OUT,
        help="Where to write the PRIVATE key PEM (git-ignored; move to secrets manager).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing private key file. Refused by default to avoid clobbering a key in use.",
    )
    args = parser.parse_args(argv)

    if os.path.exists(args.out) and not args.force:
        print(
            f"ERROR: {args.out} already exists. Refusing to overwrite a key in use. "
            "Pass --force only if you are deliberately rotating (see the runbook).",
            file=sys.stderr,
        )
        return 1

    private_pem, public_pem = generate_keypair()

    # Private key → file only (0600 where the OS supports it). Never stdout.
    fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, private_pem.encode())
    finally:
        os.close(fd)

    # Public key → stdout so it can be piped/copied into licensing.py.
    print("Private key written to:", args.out, file=sys.stderr)
    print("  → move this into CloudFulcrum's secrets manager; never commit it.", file=sys.stderr)
    print("Paste the public key below into CLOUDFULCRUM_PUBLIC_KEY in backend/app/licensing.py:", file=sys.stderr)
    print(public_pem.strip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
