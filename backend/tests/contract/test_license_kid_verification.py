"""R-1.9.1-L1 / T3 (AT-689) — key-set (kid) verification (AC2).

The trusted public keys are a CONFIG-based keyed set, not a single constant.
Verification selects the trusted key by the payload's ``kid``:

  * two trusted keys with distinct kids both verify (each key signed by its own
    private half);
  * an unknown kid yields ``invalid: unknown_key`` — distinct from the generic
    ``signature_or_format`` failure, so an operator can tell "trust/rotate this
    signing key" from "this key is corrupt or forged";
  * a known kid whose signature was made by a DIFFERENT private key still fails
    as ``signature_or_format`` (a forgery, not a rotation signal).

The key set is driven by the ``LICENSE_TRUSTED_KEYS`` env var (JSON ``{kid: pem}``)
merged over the baked-in default under ``DEFAULT_KID``. Backward compatibility:
a kid-less payload (pre-v2 / single-key mode) still resolves through the existing
``LICENSE_PUBLIC_KEY`` / baked-in single-key path. Pure crypto — no DB.
"""
from __future__ import annotations

import base64
import datetime
import json

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app import licensing
from app.licensing import (
    DEFAULT_KID,
    LicenseStatus,
    load_trusted_key_set,
    validate_license,
    verify_license_signature,
)


def _pub_pem(priv: Ed25519PrivateKey) -> str:
    return priv.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()


def _iso(days: int) -> str:
    return (datetime.date.today() + datetime.timedelta(days=days)).isoformat()


def _mint(priv: Ed25519PrivateKey, *, kid: str | None = None, expires_at: str | None = None) -> str:
    payload: dict = {
        "customer": "City National Bank",
        "license_id": "cnb-2026-001",
        "issued_at": "2026-01-01",
        "expires_at": expires_at or _iso(100),
        "term_months": 12,
        "grace_days": 14,
        "limits": {"max_workspaces": None, "enabled_packs": None},
    }
    if kid is not None:
        payload["kid"] = kid
    payload_b64 = base64.b64encode(json.dumps(payload, sort_keys=True).encode()).decode()
    sig_b64 = base64.b64encode(priv.sign(payload_b64.encode())).decode()
    return f"{payload_b64}.{sig_b64}"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Each test starts from a known-clean key-set config."""
    monkeypatch.delenv(licensing.LICENSE_TRUSTED_KEYS_ENV, raising=False)
    monkeypatch.delenv(licensing.LICENSE_PUBLIC_KEY_ENV, raising=False)


# ---------------------------------------------------------------------------
# AC2 — two trusted keys with distinct kids both verify.
# ---------------------------------------------------------------------------
def test_two_trusted_kids_each_verify(monkeypatch):
    priv_a = Ed25519PrivateKey.generate()
    priv_b = Ed25519PrivateKey.generate()
    monkeypatch.setenv(
        licensing.LICENSE_TRUSTED_KEYS_ENV,
        json.dumps({"cf-a": _pub_pem(priv_a), "cf-b": _pub_pem(priv_b)}),
    )

    res_a = validate_license(_mint(priv_a, kid="cf-a"))
    res_b = validate_license(_mint(priv_b, kid="cf-b"))

    assert res_a["status"] == LicenseStatus.VALID
    assert res_b["status"] == LicenseStatus.VALID


# ---------------------------------------------------------------------------
# AC2 — an unknown kid yields invalid: unknown_key.
# ---------------------------------------------------------------------------
def test_unknown_kid_is_unknown_key(monkeypatch):
    priv_a = Ed25519PrivateKey.generate()
    monkeypatch.setenv(
        licensing.LICENSE_TRUSTED_KEYS_ENV, json.dumps({"cf-a": _pub_pem(priv_a)})
    )

    # Signed by a trusted private key, but the payload names a kid we don't trust.
    result = validate_license(_mint(priv_a, kid="cf-does-not-exist"))
    assert result == {"status": LicenseStatus.INVALID, "reason": "unknown_key"}


def test_known_kid_wrong_signer_is_signature_or_format(monkeypatch):
    """A KNOWN kid whose signature was made by a different private key is a
    forgery — signature_or_format, not unknown_key."""
    priv_a = Ed25519PrivateKey.generate()
    attacker = Ed25519PrivateKey.generate()
    monkeypatch.setenv(
        licensing.LICENSE_TRUSTED_KEYS_ENV, json.dumps({"cf-a": _pub_pem(priv_a)})
    )

    result = validate_license(_mint(attacker, kid="cf-a"))  # kid maps to priv_a
    assert result == {"status": LicenseStatus.INVALID, "reason": "signature_or_format"}


def test_verify_license_signature_collapses_unknown_kid_to_none(monkeypatch):
    """The low-level primitive keeps its Optional[dict] contract: an unknown kid
    is None, same as any other failure. Only validate_license distinguishes it."""
    priv_a = Ed25519PrivateKey.generate()
    monkeypatch.setenv(
        licensing.LICENSE_TRUSTED_KEYS_ENV, json.dumps({"cf-a": _pub_pem(priv_a)})
    )
    assert verify_license_signature(_mint(priv_a, kid="nope")) is None


# ---------------------------------------------------------------------------
# Default kid + backward compatibility.
# ---------------------------------------------------------------------------
def test_default_kid_resolves_to_root_of_trust(monkeypatch):
    """A key issued under DEFAULT_KID verifies against the single root of trust
    (here the LICENSE_PUBLIC_KEY override), with no LICENSE_TRUSTED_KEYS set."""
    root = Ed25519PrivateKey.generate()
    monkeypatch.setenv(licensing.LICENSE_PUBLIC_KEY_ENV, _pub_pem(root))

    key_set = load_trusted_key_set()
    assert set(key_set) == {DEFAULT_KID}

    result = validate_license(_mint(root, kid=DEFAULT_KID))
    assert result["status"] == LicenseStatus.VALID


def test_kidless_payload_uses_single_key_path(monkeypatch):
    """A payload with NO kid still resolves through load_public_key (the
    LICENSE_PUBLIC_KEY / baked-in single-key path) — pre-v2 / rotation stays intact."""
    root = Ed25519PrivateKey.generate()
    monkeypatch.setenv(licensing.LICENSE_PUBLIC_KEY_ENV, _pub_pem(root))

    assert validate_license(_mint(root))["status"] == LicenseStatus.VALID  # kid-less
    # Signed by a non-trusted key with no kid → invalid (single-key mismatch).
    other = Ed25519PrivateKey.generate()
    assert validate_license(_mint(other))["status"] == LicenseStatus.INVALID


def test_explicit_public_key_ignores_kid(monkeypatch):
    """Passing an explicit public_key bypasses the key set entirely (the test /
    single-key path), so the payload's kid is irrelevant."""
    priv = Ed25519PrivateKey.generate()
    # No trusted set configured; kid would be 'unknown' if the set were consulted.
    result = validate_license(
        _mint(priv, kid="whatever"), public_key=priv.public_key()
    )
    assert result["status"] == LicenseStatus.VALID


def test_trusted_keys_override_default_kid(monkeypatch):
    """A LICENSE_TRUSTED_KEYS entry for DEFAULT_KID overrides the baked-in default."""
    override = Ed25519PrivateKey.generate()
    monkeypatch.setenv(
        licensing.LICENSE_TRUSTED_KEYS_ENV,
        json.dumps({DEFAULT_KID: _pub_pem(override)}),
    )
    result = validate_license(_mint(override, kid=DEFAULT_KID))
    assert result["status"] == LicenseStatus.VALID


# ---------------------------------------------------------------------------
# Resilience — bad config never breaks verification (degrades to default set).
# ---------------------------------------------------------------------------
def test_malformed_trusted_keys_degrades_to_default(monkeypatch):
    monkeypatch.setenv(licensing.LICENSE_TRUSTED_KEYS_ENV, "{ not valid json")
    key_set = load_trusted_key_set()
    # The baked-in default survives so verification never loses its root of trust.
    assert DEFAULT_KID in key_set


def test_invalid_pem_entry_is_skipped(monkeypatch):
    good = Ed25519PrivateKey.generate()
    monkeypatch.setenv(
        licensing.LICENSE_TRUSTED_KEYS_ENV,
        json.dumps({"cf-good": _pub_pem(good), "cf-bad": "not-a-pem"}),
    )
    key_set = load_trusted_key_set()
    assert "cf-good" in key_set
    assert "cf-bad" not in key_set  # the bad entry is skipped, not fatal


def test_issuer_and_verifier_default_kid_agree():
    """The issuer's default kid must equal the verifier's, or a default-issued key
    would select the wrong (missing) trusted key. Locks the two constants together."""
    from license.generate_license import DEFAULT_KID as ISSUER_DEFAULT_KID

    assert ISSUER_DEFAULT_KID == DEFAULT_KID
