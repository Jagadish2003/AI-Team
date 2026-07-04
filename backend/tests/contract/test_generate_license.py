"""Offline contract test for LIC-1 local issuing/verification (AT-342 / T1).

The CLI signs license payloads locally with the CloudFulcrum private key
(design §3). These tests prove the encoding + signing + verification contract
end to end: issuing (generate_license.py) and validation
(backend/app/licensing.py, T3) agree on the exact payload encoding — fully
offline, no network.

A throwaway keypair exercises the signing contract; one test also signs with the
real committed key path (if present) and verifies against the *baked-in* public
key, proving AC1 against the actually-shipped root of trust.
"""

import base64
import datetime
import json
import os
import sys

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

# Make backend/ importable (this file is backend/license/tests/..; two parents up
# is backend/), so `app.*` and `license.*` both resolve under backend.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from app.licensing import verify_license_signature  # noqa: E402
from license.generate_license import (  # noqa: E402
    DEFAULT_PRIVATE_KEY,
    build_payload,
    generate,
    sign_payload,
)


def test_build_payload_bakes_in_term_boundary():
    today = datetime.date(2026, 6, 19)
    payload = build_payload("City National Bank", "cnb-2026-001", 12, 14, today=today)
    assert payload["customer"] == "City National Bank"
    assert payload["license_id"] == "cnb-2026-001"
    assert payload["term_months"] == 12
    assert payload["grace_days"] == 14
    assert payload["issued_at"] == "2026-06-19"
    assert payload["expires_at"] == "2027-06-14"  # today + 12*30 days
    # R17-D4 Addendum A: max_systems now reserved in limits, null (unlimited) by default.
    assert payload["limits"] == {
        "max_systems": None,
        "max_workspaces": None,
        "enabled_packs": None,
    }


def test_build_payload_carries_max_systems():
    """R17-D4 Addendum A / T9: an explicit max_systems is baked into the payload."""
    payload = build_payload("Teachers Credit Union", "tcu-2027-001", 12, 14, max_systems=6)
    assert payload["limits"]["max_systems"] == 6


def test_locally_signed_key_verifies_and_parses():
    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key()

    payload = build_payload("City National Bank", "cnb-2026-001", 12, 14)
    key = sign_payload(payload, priv)

    parsed = verify_license_signature(key, public_key=pub)
    assert parsed is not None, "a correctly signed key must verify"
    assert parsed["customer"] == "City National Bank"
    assert parsed["term_months"] == 12
    assert parsed["grace_days"] == 14
    assert parsed["limits"] == {
        "max_systems": None,
        "max_workspaces": None,
        "enabled_packs": None,
    }


def test_tampered_payload_is_rejected():
    """AC2 — editing the payload after signing breaks verification."""
    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key()

    key = sign_payload(build_payload("ACME", "acme-1", 6, 14), priv)
    payload_b64, sig_b64 = key.split(".")

    tampered = json.loads(base64.b64decode(payload_b64))
    tampered["expires_at"] = "2099-01-01"  # try to extend the term
    tampered_b64 = base64.b64encode(
        json.dumps(tampered, sort_keys=True).encode()
    ).decode()
    forged_key = f"{tampered_b64}.{sig_b64}"

    assert verify_license_signature(forged_key, public_key=pub) is None


@pytest.mark.parametrize("bad", ["", "no-dot", "a.b.c", "!!!.???"])
def test_malformed_key_returns_none_never_raises(bad):
    priv = Ed25519PrivateKey.generate()
    assert verify_license_signature(bad, public_key=priv.public_key()) is None


@pytest.mark.skipif(
    not os.path.isfile(DEFAULT_PRIVATE_KEY),
    reason="CloudFulcrum private key not present (expected in CI; it is git-ignored)",
)
def test_real_key_verifies_against_baked_in_public_key():
    """AC1 — a key signed by the CloudFulcrum private key validates, fully
    offline, against the public key baked into the shipped app. Skipped where
    the private key is absent (CI), since it is git-ignored by design."""
    key = generate("City National Bank", "cnb-2026-001", 12, DEFAULT_PRIVATE_KEY, 14)
    # No public_key arg → uses the baked-in CLOUDFULCRUM_PUBLIC_KEY constant.
    parsed = verify_license_signature(key)
    assert parsed is not None, "issued key must verify against the baked-in public key"
    assert parsed["customer"] == "City National Bank"
