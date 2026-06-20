#!/usr/bin/env python3
"""LIC-1 — DEV/TEST ONLY: mint sample license keys for every state.

This is a developer convenience for manually testing the License page and gate
WITHOUT the real CloudFulcrum private key. It generates a throwaway Ed25519
keypair, mints one key per license state, and prints the throwaway PUBLIC key.

To use the minted keys, you must temporarily point the app at this throwaway
public key (the real keys are signed by CloudFulcrum, which you don't have
locally):

  1. python backend/license/dev_mint_test_keys.py
  2. Copy the printed "DEV PUBLIC KEY" into CLOUDFULCRUM_PUBLIC_KEY in
     backend/app/licensing.py  (TEMPORARY — do not commit this change)
  3. Restart the backend
  4. Paste the keys from backend/license/test_keys/ into the admin License page
  5. git checkout backend/app/licensing.py   (restore the real public key)

NOT shipped to customers: lives under backend/license/, which backend/.dockerignore
excludes from the image. Output is written to backend/license/test_keys/ (git-ignored).
"""
from __future__ import annotations

import base64
import datetime
import json
import os
import sys

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

# Reuse the real CLI's canonical encoding so dev keys are byte-identical in shape
# to production keys.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from generate_license import build_payload, encode_payload, sign_payload  # noqa: E402

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_keys")


def _mint(priv, *, days_to_expiry: int, grace_days: int = 14) -> str:
    today = datetime.date.today()
    payload = build_payload("City National Bank", "cnb-2026-001", 12, grace_days, today=today)
    payload["expires_at"] = (today + datetime.timedelta(days=days_to_expiry)).isoformat()
    return sign_payload(payload, priv)


def _tamper(key: str) -> str:
    payload_b64, sig_b64 = key.split(".")
    forged = json.loads(base64.b64decode(payload_b64))
    forged["expires_at"] = "2099-01-01"  # forge a far-future expiry
    return encode_payload(forged) + "." + sig_b64


def main() -> int:
    os.makedirs(OUT_DIR, exist_ok=True)
    priv = Ed25519PrivateKey.generate()
    pub_pem = (
        priv.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
        .strip()
    )

    keys = {
        "valid": _mint(priv, days_to_expiry=120),       # within term
        "grace": _mint(priv, days_to_expiry=-7),        # expired 7d ago, 14d grace
        "readonly": _mint(priv, days_to_expiry=-30),    # past grace
    }
    keys["tampered"] = _tamper(_mint(priv, days_to_expiry=120))  # forged → invalid

    for name, key in keys.items():
        path = os.path.join(OUT_DIR, f"key_{name}.txt")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(key)

    print("=== DEV PUBLIC KEY — paste into CLOUDFULCRUM_PUBLIC_KEY (TEMP, do not commit) ===")
    print(pub_pem)
    print()
    print(f"Minted test keys in: {OUT_DIR}")
    for name in keys:
        print(f"  key_{name}.txt")
    print()
    print("Then restart the backend and paste each key into the License page.")
    print("Restore the real key when done:  git checkout backend/app/licensing.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
