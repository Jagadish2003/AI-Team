"""Unit tests — LIC-1 review fix: LICENSE_PUBLIC_KEY env rotation hook.

`licensing.load_public_key()` must resolve the trusted public key from the
`LICENSE_PUBLIC_KEY` env var when set (rotation without a code change/release),
falling back to the baked-in constant otherwise. Pure crypto — no DB.
"""
from __future__ import annotations

import base64
import datetime
import json

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app import licensing
from app.licensing import DEFAULT_KID


def _pub_pem(priv: Ed25519PrivateKey) -> str:
    return priv.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()


def _mint(priv: Ed25519PrivateKey, *, expires_at: str) -> str:
    """A v2-shaped key (org_id + default kid) so it clears the T4 version gate;
    the kid resolves through the LICENSE_PUBLIC_KEY single-key path under
    DEFAULT_KID, exactly as a real default-kid key does."""
    payload = {
        "customer": "City National Bank",
        "license_id": "cnb-2026-001",
        "issued_at": "2026-01-01",
        "expires_at": expires_at,
        "term_months": 12,
        "grace_days": 14,
        "org_id": "org-cnb",
        "kid": DEFAULT_KID,
        "limits": {"max_workspaces": None, "enabled_packs": None},
    }
    payload_b64 = base64.b64encode(json.dumps(payload, sort_keys=True).encode()).decode()
    sig_b64 = base64.b64encode(priv.sign(payload_b64.encode())).decode()
    return f"{payload_b64}.{sig_b64}"


def test_load_public_key_falls_back_to_constant(monkeypatch):
    monkeypatch.delenv(licensing.LICENSE_PUBLIC_KEY_ENV, raising=False)
    # Baked-in constant loads as a valid Ed25519 public key.
    assert licensing.load_public_key() is not None


def test_load_public_key_uses_env_override(monkeypatch):
    priv = Ed25519PrivateKey.generate()
    monkeypatch.setenv(licensing.LICENSE_PUBLIC_KEY_ENV, _pub_pem(priv))

    key = licensing.load_public_key()
    payload_b64 = base64.b64encode(b'{"x":1}').decode()
    # Verifies a signature from the env keypair → the env key is in use.
    key.verify(priv.sign(payload_b64.encode()), payload_b64.encode())


def test_validate_license_honours_env_public_key(monkeypatch):
    priv = Ed25519PrivateKey.generate()
    monkeypatch.setenv(licensing.LICENSE_PUBLIC_KEY_ENV, _pub_pem(priv))
    future = (datetime.date.today() + datetime.timedelta(days=100)).isoformat()

    # validate_license() with no explicit key resolves the env public key.
    result = licensing.validate_license(_mint(priv, expires_at=future))
    assert result["status"] == "valid"


def test_validate_license_rejects_key_not_signed_by_env_keypair(monkeypatch):
    env_priv = Ed25519PrivateKey.generate()
    other_priv = Ed25519PrivateKey.generate()  # not the trusted key
    monkeypatch.setenv(licensing.LICENSE_PUBLIC_KEY_ENV, _pub_pem(env_priv))
    future = (datetime.date.today() + datetime.timedelta(days=100)).isoformat()

    result = licensing.validate_license(_mint(other_priv, expires_at=future))
    assert result["status"] == "invalid"
