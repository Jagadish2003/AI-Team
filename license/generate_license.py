#!/usr/bin/env python3
"""LIC-1 (AT-342 / T1) — CloudFulcrum-internal license issuing CLI.

API-based variant: CloudFulcrum runs a remote signing service that holds the
private Ed25519 key. This tool does NOT hold or load a private key — it builds
the request payload, calls the issuing API, and prints the returned signed
license key string. This is safer than a local signing CLI: the private key
never touches a laptop or this repo.

This directory (``license/``) is CloudFulcrum-internal and is excluded from the
customer build/Docker image (AC10) — the backend Docker build context is
``backend/``, so a top-level ``license/`` is never copied into the image.

Confidential values are read from the environment, never hardcoded:
  * LICENSE_API_URL    — the issuing API endpoint (confidential)
  * LICENSE_API_TOKEN  — bearer token for the API (confidential)
Put them in an untracked file (e.g. license/.env or backend/.env) or export
them in your shell. They must never be committed.

Usage:
  python license/generate_license.py \
    --customer 'City National Bank' \
    --license-id cnb-2026-001 \
    --term-months 12 \
    --grace-days 14
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

ALLOWED_TERMS = (3, 6, 12)


def build_payload(
    customer: str,
    license_id: str,
    term_months: int,
    grace_days: int = 14,
) -> dict:
    """Assemble the request body sent to the issuing API.

    The API computes ``issued_at``/``expires_at`` (today and today +
    term_months*30 days) and signs the canonical payload. ``limits`` are
    reserved (null) in v1 — present in the signed payload from day one so seat/
    pack limits can be enforced later without changing the key format.
    """
    return {
        "customer": customer,
        "license_id": license_id,
        "term_months": term_months,
        "grace_days": grace_days,
        "limits": {"max_workspaces": None, "enabled_packs": None},
    }


def _load_env_file() -> None:
    """Best-effort load of license/.env then backend/.env (no dependency)."""
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(here, ".env"),
        os.path.join(here, "..", "backend", ".env"),
    ]
    for path in candidates:
        if not os.path.isfile(path):
            continue
        with open(path, "r", encoding="utf-8-sig") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def call_issuing_api(payload: dict) -> dict:
    """POST the payload to the issuing API and return the parsed JSON response.

    Raises RuntimeError with the API's response body on a non-2xx status so the
    caller can see exactly which fields the API rejected.
    """
    url = os.environ.get("LICENSE_API_URL")
    token = os.environ.get("LICENSE_API_TOKEN")
    if not url:
        raise RuntimeError(
            "LICENSE_API_URL is not set. Put it in license/.env or backend/.env "
            "(untracked), or export it. It must never be committed."
        )

    data = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        raise RuntimeError(f"Issuing API returned {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not reach issuing API: {exc.reason}") from exc


def extract_key(response: dict) -> str:
    """Pull the signed key string out of the API response, tolerating a few
    common field names. Adjust here once the real API contract is confirmed."""
    for field in ("license_key", "key", "licenseKey", "license"):
        if isinstance(response.get(field), str):
            return response[field]
    raise RuntimeError(
        "Could not find the license key in the API response. "
        f"Response keys were: {sorted(response)}"
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Issue a signed AgentIQ license key via the CloudFulcrum API.")
    parser.add_argument("--customer", required=True)
    parser.add_argument("--license-id", required=True)
    parser.add_argument("--term-months", type=int, required=True, choices=ALLOWED_TERMS)
    parser.add_argument("--grace-days", type=int, default=14)
    args = parser.parse_args(argv)

    _load_env_file()
    payload = build_payload(args.customer, args.license_id, args.term_months, args.grace_days)
    try:
        response = call_issuing_api(payload)
        key = extract_key(response)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    # The signed key string is the only thing printed to stdout, so it can be
    # piped/copied cleanly. Everything else goes to stderr.
    print(key)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
