#!/usr/bin/env python3
"""LIC-1 (AT-343 / T2) — obtain the CloudFulcrum license keypair.

The signing keypair is provisioned by DevOps and served by the AWS service at
``LICENSE_API_URL`` (configured in ``backend/.env``). This tool fetches that
official keypair and writes it to the two git-ignored PEM files the rest of the
license tooling reads by relative path:

  * ``agentiq_lic_private_key.pem``  — the private signing key (0600). Secret.
  * ``agentiq_lic_public_key.pem``   — the public key (safe; also baked into
    ``app/licensing.py`` as ``CLOUDFULCRUM_PUBLIC_KEY``).

Why fetch instead of generate: the app verifies against a SPECIFIC public key. A
freshly *generated* random keypair would not match it, so keys it signs would be
rejected. Fetching from ``LICENSE_API_URL`` gives every developer the SAME,
correct pair that matches the shipped public key. (Use ``--local`` only when you
deliberately want a throwaway keypair for isolated testing.)

Security: key MATERIAL is never printed — only the paths written. The PEM files
are git-ignored (``*.pem``) and must never be committed. Treat the private key
like ``.env``: share it only over a secure channel, never in the repo.

Usage (run from the repo root, venv active):
  python backend/license/generate_keypair.py                 # fetch from LICENSE_API_URL
  python backend/license/generate_keypair.py --force         # overwrite existing files
  python backend/license/generate_keypair.py --local         # throwaway local pair (testing)

Rotation: see "Key rotation runbook" in backend/license/README.md.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import (
    load_pem_private_key,
    load_pem_public_key,
)

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND_DIR = os.path.abspath(os.path.join(_HERE, ".."))

DEFAULT_PRIVATE_OUT = os.path.join(_HERE, "agentiq_lic_private_key.pem")
DEFAULT_PUBLIC_OUT = os.path.join(_HERE, "agentiq_lic_public_key.pem")

# ---------------------------------------------------------------------------
# LICENSE_API_URL contract — ADJUST THESE to match your DevOps key endpoint.
# (Defaults assume a GET returning JSON with the two PEM strings. If your API
# differs, change only the four values below — no other code changes needed.)
# ---------------------------------------------------------------------------
LICENSE_API_URL_ENV = "LICENSE_API_URL"       # read from backend/.env at runtime
LICENSE_API_TOKEN_ENV = "LICENSE_API_TOKEN"   # optional bearer token (if the API needs auth)
HTTP_METHOD = "GET"                            # "GET" or "POST"
REQUEST_BODY = None                            # e.g. '{"action":"get-keys"}' for a POST; None for GET
# JSON field names the response may use for each PEM (first match wins). The
# response may also nest them under a "data"/"keys"/"result" wrapper.
PRIVATE_KEY_FIELDS = ("private_key", "private_key_pem", "privateKey", "privateKeyPem", "privatePem")
PUBLIC_KEY_FIELDS = ("public_key", "public_key_pem", "publicKey", "publicKeyPem", "publicPem")
# ---------------------------------------------------------------------------


def _load_env() -> None:
    """Load backend/.env so LICENSE_API_URL is available (override=False)."""
    from dotenv import load_dotenv

    load_dotenv(os.path.join(_BACKEND_DIR, ".env"), override=False)


def _first_str(d: dict, keys) -> str | None:
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v
    return None


def fetch_keys_from_api(url: str, token: str | None = None, timeout: int = 30):
    """Fetch (private_pem, public_pem) from LICENSE_API_URL. Never logs the values."""
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = REQUEST_BODY.encode() if REQUEST_BODY else None
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=HTTP_METHOD, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec - operator-configured URL
        raw = resp.read().decode()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"{LICENSE_API_URL_ENV} did not return JSON — adjust the parser/contract "
            "constants at the top of generate_keypair.py to its response shape."
        ) from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"{LICENSE_API_URL_ENV} returned a non-object JSON payload.")
    # Unwrap a common envelope if present.
    for wrapper in ("data", "keys", "result"):
        inner = payload.get(wrapper)
        if isinstance(inner, dict):
            payload = inner
            break
    priv = _first_str(payload, PRIVATE_KEY_FIELDS)
    pub = _first_str(payload, PUBLIC_KEY_FIELDS)
    if not priv:
        raise RuntimeError(
            f"no private-key field {PRIVATE_KEY_FIELDS} in the {LICENSE_API_URL_ENV} "
            "response — set PRIVATE_KEY_FIELDS to the actual field name."
        )
    # Tolerate JSON-escaped newlines in the PEM strings.
    priv = priv.replace("\\n", "\n")
    if pub:
        pub = pub.replace("\\n", "\n")
    return priv, pub


def _validate_pair(private_pem: str, public_pem: str | None) -> str:
    """Ensure the fetched key is Ed25519 and the public matches; return the public PEM.

    If the API returned no public key, derive it from the private key. Never
    prints key material — only raises on a mismatch.
    """
    key = load_pem_private_key(private_pem.encode(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise RuntimeError("fetched private key is not an Ed25519 key.")
    derived = (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    if not public_pem:
        return derived
    pub = load_pem_public_key(public_pem.encode())
    if not isinstance(pub, Ed25519PublicKey):
        raise RuntimeError("fetched public key is not an Ed25519 key.")
    got = pub.public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()
    if got.strip() != derived.strip():
        raise RuntimeError("fetched public key does NOT match the fetched private key.")
    return public_pem


def _generate_local_pair() -> tuple[str, str]:
    """A throwaway local Ed25519 pair (``--local``; will NOT match the app's key)."""
    priv = Ed25519PrivateKey.generate()
    private_pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = (
        priv.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    return private_pem, public_pem


def _write_secret(path: str, pem: str, mode: int) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        os.write(fd, pem.encode())
    finally:
        os.close(fd)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Fetch the CloudFulcrum license keypair from LICENSE_API_URL and write the two PEM files."
    )
    parser.add_argument("--private-out", default=DEFAULT_PRIVATE_OUT, help="Path for the private key PEM.")
    parser.add_argument("--public-out", default=DEFAULT_PUBLIC_OUT, help="Path for the public key PEM.")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing key files. Refused by default to avoid clobbering keys in use.",
    )
    parser.add_argument(
        "--local",
        action="store_true",
        help="Generate a THROWAWAY local keypair instead of fetching (testing only; won't match the app's key).",
    )
    args = parser.parse_args(argv)

    if os.path.exists(args.private_out) and not args.force:
        print(
            f"ERROR: {args.private_out} already exists. Pass --force to overwrite "
            "(only when deliberately rotating / re-fetching).",
            file=sys.stderr,
        )
        return 1

    if args.local:
        private_pem, public_pem = _generate_local_pair()
        print("(--local) generated a throwaway keypair - it will NOT match the app's public key.", file=sys.stderr)
    else:
        _load_env()
        url = os.getenv(LICENSE_API_URL_ENV)
        if not url:
            print(
                f"ERROR: {LICENSE_API_URL_ENV} is not set (add it to backend/.env), or use --local.",
                file=sys.stderr,
            )
            return 1
        token = os.getenv(LICENSE_API_TOKEN_ENV)
        try:
            private_pem, public_pem = fetch_keys_from_api(url, token)
            public_pem = _validate_pair(private_pem, public_pem)
        except (urllib.error.URLError, urllib.error.HTTPError, RuntimeError, ValueError) as exc:
            print(f"ERROR fetching keys from {LICENSE_API_URL_ENV}: {exc}", file=sys.stderr)
            return 1

    _write_secret(args.private_out, private_pem, 0o600)
    _write_secret(args.public_out, public_pem, 0o644)

    print(f"Wrote private key -> {args.private_out} (mode 0600)", file=sys.stderr)
    print(f"Wrote public  key -> {args.public_out}", file=sys.stderr)
    print("Both files are git-ignored (*.pem). NEVER commit them; share the private key only over a secure channel.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
