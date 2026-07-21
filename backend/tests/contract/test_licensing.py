"""Unit tests for the LIC-1 offline validation core (AT-344 / T3).

validate_license() verifies against the baked-in public key, whose private half
lives only on the CloudFulcrum issuing service. To exercise the status logic
without that private key, these tests generate a throwaway Ed25519 keypair, sign
payloads with the exact issuing encoding, and pass the throwaway public key into
validate_license(public_key=...).

Expiry dates are computed relative to date.today() so the date-boundary cases
(valid / last-day / mid-grace / first-day-past-grace) are deterministic without
mocking the clock.
"""

import base64
import datetime
import json

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.licensing import LicenseStatus, validate_license


@pytest.fixture
def keypair():
    priv = Ed25519PrivateKey.generate()
    return priv, priv.public_key()


def _sign(priv, *, expires_at, grace_days=14, customer="City National Bank",
          license_id="cnb-2026-001", term_months=12, deployment_type=None):
    """Replicate the issuing scheme exactly (sort_keys=True, base64, Ed25519).

    ``deployment_type`` is added to the payload only when supplied, so the default
    call still exercises a payload that omits it (the pre-v2 shape)."""
    payload = {
        "customer": customer,
        "license_id": license_id,
        "issued_at": "2026-06-19",
        "expires_at": expires_at,
        "term_months": term_months,
        "grace_days": grace_days,
        "limits": {"max_workspaces": None, "enabled_packs": None},
    }
    if deployment_type is not None:
        payload["deployment_type"] = deployment_type
    payload_b64 = base64.b64encode(json.dumps(payload, sort_keys=True).encode()).decode()
    sig_b64 = base64.b64encode(priv.sign(payload_b64.encode())).decode()
    return f"{payload_b64}.{sig_b64}"


def _iso(days_from_today):
    return (datetime.date.today() + datetime.timedelta(days=days_from_today)).isoformat()


def test_valid_within_term(keypair):
    priv, pub = keypair
    key = _sign(priv, expires_at=_iso(100))
    result = validate_license(key, public_key=pub)
    assert result["status"] == LicenseStatus.VALID
    assert result["customer"] == "City National Bank"
    assert result["days_remaining"] == 100
    assert result["payload"]["license_id"] == "cnb-2026-001"


def test_last_day_of_term_is_valid(keypair):
    """expires_at == today is still within term (inclusive boundary)."""
    priv, pub = keypair
    key = _sign(priv, expires_at=_iso(0))
    result = validate_license(key, public_key=pub)
    assert result["status"] == LicenseStatus.VALID
    assert result["days_remaining"] == 0


def test_mid_grace(keypair):
    """7 days past a 14-day grace expiry -> grace, still reported."""
    priv, pub = keypair
    key = _sign(priv, expires_at=_iso(-7), grace_days=14)
    result = validate_license(key, public_key=pub)
    assert result["status"] == LicenseStatus.GRACE
    assert result["days_remaining"] == -7


def test_first_day_past_grace_is_readonly(keypair):
    """15 days past expiry with 14-day grace -> read-only."""
    priv, pub = keypair
    key = _sign(priv, expires_at=_iso(-15), grace_days=14)
    result = validate_license(key, public_key=pub)
    assert result["status"] == LicenseStatus.READONLY


def test_tampered_signature_is_invalid(keypair):
    """AC2 — editing expires_at after signing breaks verification."""
    priv, pub = keypair
    key = _sign(priv, expires_at=_iso(-15), grace_days=14)
    payload_b64, sig_b64 = key.split(".")
    tampered = json.loads(base64.b64decode(payload_b64))
    tampered["expires_at"] = _iso(3650)  # try to extend far into the future
    tampered_b64 = base64.b64encode(json.dumps(tampered, sort_keys=True).encode()).decode()
    forged = f"{tampered_b64}.{sig_b64}"

    result = validate_license(forged, public_key=pub)
    assert result == {"status": LicenseStatus.INVALID, "reason": "signature_or_format"}


@pytest.mark.parametrize("bad", ["", "no-dot", "a.b.c", "!!!.???", "x." ])
def test_malformed_string_is_invalid_never_raises(keypair, bad):
    _, pub = keypair
    result = validate_license(bad, public_key=pub)
    assert result == {"status": LicenseStatus.INVALID, "reason": "signature_or_format"}


def test_wrong_key_is_invalid(keypair):
    """A key signed by a different private key fails against this public key."""
    priv, pub = keypair
    other = Ed25519PrivateKey.generate()
    key = _sign(other, expires_at=_iso(100))
    result = validate_license(key, public_key=pub)
    assert result == {"status": LicenseStatus.INVALID, "reason": "signature_or_format"}


def test_signature_valid_but_payload_missing_expiry_is_invalid(keypair):
    """A correctly signed but structurally bad payload is still invalid."""
    priv, pub = keypair
    payload_b64 = base64.b64encode(json.dumps({"customer": "X"}, sort_keys=True).encode()).decode()
    sig_b64 = base64.b64encode(priv.sign(payload_b64.encode())).decode()
    result = validate_license(f"{payload_b64}.{sig_b64}", public_key=pub)
    assert result == {"status": LicenseStatus.INVALID, "reason": "signature_or_format"}


def test_deployment_type_surfaced_at_top_level(keypair):
    """R-1.9.1-L1 / T1 (AC5): deployment_type is parsed from the payload and
    lifted to the top level of the result so the status API exposes it."""
    priv, pub = keypair
    key = _sign(priv, expires_at=_iso(100), deployment_type="customer_hosted")
    result = validate_license(key, public_key=pub)
    assert result["status"] == LicenseStatus.VALID
    assert result["deployment_type"] == "customer_hosted"
    # Still readable via the raw payload too.
    assert result["payload"]["deployment_type"] == "customer_hosted"


def test_deployment_type_none_for_pre_v2_payload(keypair):
    """A pre-v2 key that carries no deployment_type resolves to None, not an error."""
    priv, pub = keypair
    key = _sign(priv, expires_at=_iso(100))  # no deployment_type
    result = validate_license(key, public_key=pub)
    assert result["status"] == LicenseStatus.VALID
    assert result["deployment_type"] is None


def test_default_uses_baked_in_key_and_never_raises():
    """Calling with no public_key uses the shipped constant; garbage -> invalid."""
    result = validate_license("garbage-not-a-key")
    assert result == {"status": LicenseStatus.INVALID, "reason": "signature_or_format"}
