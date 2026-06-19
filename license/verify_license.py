#!/usr/bin/env python3
"""LIC-1 helper — verify a license key offline against the baked-in public key.

Use this to confirm a key issued by the API actually validates against the
public key shipped in backend/app/licensing.py (AC1), and that a tampered key
is rejected (AC2). Fully offline — no network call.

Usage:
  python license/verify_license.py --key '<payload_b64>.<sig_b64>'
"""

from __future__ import annotations

import argparse
import os
import sys

# Make the repo root importable so we verify against the SAME constant the app
# ships, rather than a copy.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from backend.app.licensing import verify_license_signature  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Offline-verify an AgentIQ license key.")
    parser.add_argument("--key", required=True, help="The license key string (payload.signature).")
    args = parser.parse_args(argv)

    payload = verify_license_signature(args.key)
    if payload is None:
        print("INVALID — signature did not verify against the baked-in public key.", file=sys.stderr)
        return 1

    print("VALID")
    print(f"  customer    : {payload.get('customer')}")
    print(f"  license_id  : {payload.get('license_id')}")
    print(f"  issued_at   : {payload.get('issued_at')}")
    print(f"  expires_at  : {payload.get('expires_at')}")
    print(f"  term_months : {payload.get('term_months')}")
    print(f"  grace_days  : {payload.get('grace_days')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
