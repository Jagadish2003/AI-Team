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

import pytest  # noqa: E402  (re-imported here for the parametrize below)

from app.licensing import verify_license_signature  # noqa: E402
from license.generate_license import (  # noqa: E402
    DEFAULT_DEPLOYMENT_TYPE,
    DEFAULT_KID,
    DEFAULT_PRIVATE_KEY,
    PAYLOAD_VERSION,
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


def test_build_payload_defaults_org_name_to_customer():
    """R17-D4 Addendum A / T12 (§2): org_name is present and defaults to customer.

    The field is purely additive to the payload (structure otherwise unchanged);
    limits are untouched, so pre-addendum behaviour is preserved.
    """
    payload = build_payload("Teachers Credit Union", "tcu-2027-001", 12, 14)
    assert payload["org_name"] == "Teachers Credit Union"
    assert payload["customer"] == "Teachers Credit Union"
    # No key-format change: the limits block is unchanged by adding org_name.
    assert payload["limits"] == {
        "max_systems": None,
        "max_workspaces": None,
        "enabled_packs": None,
    }


def test_build_payload_carries_explicit_org_name():
    """R17-D4 Addendum A / T12: an explicit display name distinct from customer."""
    payload = build_payload(
        "Teachers Credit Union",
        "tcu-2027-001",
        12,
        14,
        org_name="Teachers CU",
    )
    assert payload["org_name"] == "Teachers CU"
    assert payload["customer"] == "Teachers Credit Union"


def test_org_name_survives_signing_and_verification():
    """R17-D4 Addendum A / T12: org_name flows through signing into the validated
    payload, so the display-name resolver can read it (AC15)."""
    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key()

    payload = build_payload(
        "Teachers Credit Union", "tcu-2027-001", 12, 14, org_name="Teachers CU"
    )
    key = sign_payload(payload, priv)

    parsed = verify_license_signature(key, public_key=pub)
    assert parsed is not None
    assert parsed["org_name"] == "Teachers CU"


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


# ---------------------------------------------------------------------------
# R-1.9.1-L1 / T1 (AT-687) — payload v2 schema.
# ---------------------------------------------------------------------------
def test_v2_payload_carries_new_fields_and_version():
    """T1: a v2 payload stamps payload_version and the four new fields, with sane
    defaults, while every existing field is unchanged (purely additive)."""
    today = datetime.date(2026, 6, 19)
    payload = build_payload(
        "City National Bank",
        "cnb-2026-001",
        12,
        14,
        today=today,
    )
    # New v2 fields.
    assert payload["payload_version"] == PAYLOAD_VERSION == 2
    assert payload["kid"] == DEFAULT_KID
    assert payload["deployment_type"] == DEFAULT_DEPLOYMENT_TYPE == "saas"
    assert payload["report_key"] is None
    # org_id defaults to customer when not given (always populated for a v2 key).
    assert payload["org_id"] == "City National Bank"
    # Existing fields unchanged.
    assert payload["customer"] == "City National Bank"
    assert payload["org_name"] == "City National Bank"
    assert payload["term_months"] == 12
    assert payload["grace_days"] == 14
    assert payload["issued_at"] == "2026-06-19"
    assert payload["expires_at"] == "2027-06-14"
    assert payload["limits"] == {
        "max_systems": None,
        "max_workspaces": None,
        "enabled_packs": None,
    }


def test_v2_payload_carries_explicit_fields():
    """An explicit org_id, kid, deployment_type, and report_key are baked in."""
    payload = build_payload(
        "Teachers Credit Union",
        "tcu-2027-001",
        12,
        14,
        org_id="org-tcu-prod",
        kid="cf-2027-2",
        deployment_type="customer_hosted",
        report_key="rk-abc123",
    )
    assert payload["org_id"] == "org-tcu-prod"
    assert payload["kid"] == "cf-2027-2"
    assert payload["deployment_type"] == "customer_hosted"
    assert payload["report_key"] == "rk-abc123"


def test_build_payload_rejects_unknown_deployment_type():
    """Issuer-side guard: an out-of-set deployment_type is rejected at build time."""
    with pytest.raises(ValueError):
        build_payload("ACME", "acme-1", 6, 14, deployment_type="on_prem")


def test_v2_fields_survive_signing_and_verification():
    """T1: the new fields flow through signing into the verified payload, so the
    verifier (T2–T4) and status API (AC5) can read them."""
    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key()

    payload = build_payload(
        "Teachers Credit Union",
        "tcu-2027-001",
        12,
        14,
        org_id="org-tcu-prod",
        kid="cf-2027-2",
        deployment_type="customer_hosted",
        report_key="rk-abc123",
    )
    key = sign_payload(payload, priv)

    parsed = verify_license_signature(key, public_key=pub)
    assert parsed is not None
    assert parsed["payload_version"] == 2
    assert parsed["org_id"] == "org-tcu-prod"
    assert parsed["kid"] == "cf-2027-2"
    assert parsed["deployment_type"] == "customer_hosted"
    assert parsed["report_key"] == "rk-abc123"


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
