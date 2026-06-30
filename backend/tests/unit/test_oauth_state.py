"""Unit tests for the OAuth state encode/verify helper — R17-D3 / AT-447 (T2).

These cover the helper in isolation (no FastAPI / DB): the org is carried in the
state, the signature makes it tamper-evident, and verification is fail-closed.
"""
import pytest

from app.auth.oauth_state import decode_state, encode_state


def test_encode_decode_roundtrip_carries_org_and_nonce():
    """T2-AC1: the org and nonce survive an encode → decode round-trip intact."""
    state = encode_state("org-123", "nonce-abc")
    decoded = decode_state(state)
    assert decoded == {"org_id": "org-123", "nonce": "nonce-abc"}


def test_encode_requires_org_and_nonce():
    """An unbound state (missing org or nonce) must never be produced."""
    with pytest.raises(ValueError):
        encode_state("", "nonce")
    with pytest.raises(ValueError):
        encode_state("org", "")


def test_each_encode_is_self_consistent_but_org_specific():
    """Two different orgs over the same nonce produce different, non-interchangeable
    states (the signature binds the org)."""
    s_a = encode_state("org-a", "shared-nonce")
    s_b = encode_state("org-b", "shared-nonce")
    assert s_a != s_b
    assert decode_state(s_a)["org_id"] == "org-a"
    assert decode_state(s_b)["org_id"] == "org-b"


def test_tampered_org_fails_verification():
    """T2-AC2/AC3: editing the org segment without re-signing is rejected."""
    state = encode_state("org-aaaa", "nonce-1")
    tampered = state.replace("org-aaaa", "org-bbbb", 1)
    assert tampered != state
    assert decode_state(tampered) is None


def test_tampered_nonce_fails_verification():
    """Editing the nonce segment without re-signing is rejected."""
    state = encode_state("org-1", "nonce-aaaa")
    tampered = state.replace("nonce-aaaa", "nonce-bbbb", 1)
    assert decode_state(tampered) is None


def test_bad_signature_fails_verification():
    state = encode_state("org-1", "nonce-1")
    body, _sig = state.rsplit(".", 1)
    assert decode_state(f"{body}.deadbeef") is None


@pytest.mark.parametrize(
    "bad",
    [
        None,
        "",
        "not-a-state",
        "only.two",
        "a.b.",       # empty signature
        ".b.c",       # empty org
        "a..c",       # empty nonce
    ],
)
def test_malformed_states_return_none(bad):
    """A missing or malformed state is fail-closed (None), never a partial parse."""
    assert decode_state(bad) is None


def test_org_id_with_dots_is_preserved():
    """rsplit-based parsing recovers an org_id that itself contains dots (the nonce
    and hex signature never do), so the binding is robust to dotted org ids."""
    state = encode_state("a.b.c", "nonce-1")
    decoded = decode_state(state)
    assert decoded == {"org_id": "a.b.c", "nonce": "nonce-1"}
