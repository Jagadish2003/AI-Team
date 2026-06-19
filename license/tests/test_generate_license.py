"""Offline contract test for LIC-1 issuing/verification (AT-342 / T1).

Per the ticket, T1 can be exercised before the real keypair (T2) exists by
using a throwaway local keypair. This proves the encoding + signing +
verification contract end to end, so issuing (the API) and validation
(backend/app/licensing.py, T3) agree on the exact payload encoding — without
needing the real private key or the network.
"""

import base64
import json
import os
import sys

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

# Make repo root importable.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from backend.app.licensing import verify_license_signature  # noqa: E402
from license.generate_license import build_payload  # noqa: E402


def _sign_like_the_api(inputs: dict, private_key: Ed25519PrivateKey) -> str:
    """Replicate the issuing scheme exactly: the API fills issued_at/expires_at
    and signs base64(json.dumps(payload, sort_keys=True))."""
    payload = dict(inputs)
    payload["issued_at"] = "2026-06-19"
    payload["expires_at"] = "2027-06-19"
    payload_b64 = base64.b64encode(
        json.dumps(payload, sort_keys=True).encode()
    ).decode()
    sig_b64 = base64.b64encode(private_key.sign(payload_b64.encode())).decode()
    return f"{payload_b64}.{sig_b64}"


def test_generated_key_verifies_and_parses():
    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key()

    inputs = build_payload("City National Bank", "cnb-2026-001", 12, 14)
    key = _sign_like_the_api(inputs, priv)

    payload = verify_license_signature(key, public_key=pub)

    assert payload is not None, "a correctly signed key must verify"
    assert payload["customer"] == "City National Bank"
    assert payload["license_id"] == "cnb-2026-001"
    assert payload["term_months"] == 12
    assert payload["grace_days"] == 14
    assert payload["limits"] == {"max_workspaces": None, "enabled_packs": None}
    assert payload["expires_at"] == "2027-06-19"


def test_tampered_payload_is_rejected():
    """AC2 — editing the payload after signing breaks verification."""
    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key()

    key = _sign_like_the_api(build_payload("ACME", "acme-1", 6, 14), priv)
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
