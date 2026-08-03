"""2.0-C2 T1 (AT-831) — pack certification metadata + signature verification.

Sub-task scope: *level, certifying entity, review date, reviewed-against platform
version, and the certification's scope; cryptographically signed by CloudFulcrum for
Certified and Partner levels so the label cannot be self-applied.*

Parent-story criterion discharged here:

  * AC1 — certification metadata is signature-verified; a pack claiming Certified
    without a valid signature is treated as Community.

Also pinned: the shipped declarations stay honest (every registered pack declares a
complete certification block, every shipped signature verifies against the shipped
trust anchor, and no shipped pack is review-due on the current platform version).
Those structural tests are what make the badge trustworthy — they fail the build if
a future pack ships an unsigned or edited claim, rather than letting the downgrade
surface in front of a customer.

Pure-Python and offline — no DB and no credentials. The signing tests mint an
EPHEMERAL key pair, so CI never needs the release private key.
"""
from __future__ import annotations

import base64
import copy
import json
import os

import pytest

os.environ.setdefault("INGEST_MODE", "offline")

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import (  # noqa: E402
    Ed25519PrivateKey,
)

from discovery.packs import pack_certification  # noqa: E402
from discovery.packs.pack_certification import (  # noqa: E402
    ALGORITHM_ED25519,
    CERTIFICATION_LEVELS,
    CLOUDFULCRUM,
    CLOUDFULCRUM_SIGNING_KEYS,
    LEVEL_CERTIFIED,
    LEVEL_COMMUNITY,
    LEVEL_LABELS,
    LEVEL_PARTNER,
    REASON_INVALID_LEVEL,
    REASON_MISSING_METADATA,
    REASON_SIGNATURE_INVALID,
    REASON_SIGNATURE_MALFORMED,
    REASON_SIGNATURE_MISSING,
    REASON_SIGNATURE_UNKNOWN_KEY,
    REASON_SIGNATURE_UNSUPPORTED_ALGORITHM,
    REVIEW_DUE_PLATFORM_MOVED,
    SIGNATURE_PAYLOAD_VERSION,
    SIGNATURE_REQUIRED_LEVELS,
    TRUSTED_KEYS_ENV_VAR,
    canonical_payload_bytes,
    certification_payload,
    certification_summary,
    certify_pack_selection,
    get_certification_level,
    get_pack_certification,
    meets_minimum_level,
    set_trusted_signing_keys,
    sign_certification,
    trusted_key_ids,
    trusted_signing_keys,
    verify_certification_signature,
)
from discovery.packs.pack_config import (  # noqa: E402
    CERTIFICATION_KEY,
    DEFAULT_PACK,
    PACK_REGISTRY,
    get_pack_certification_declaration,
    list_packs,
)
from discovery.packs.platform_capabilities import PLATFORM_VERSION  # noqa: E402

TEST_KEY_ID = "test-signing-key"


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def ephemeral_key():
    """A throwaway Ed25519 key pair: ``(seed_bytes, base64_public_key)``."""
    private_key = Ed25519PrivateKey.generate()
    seed = private_key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return seed, base64.b64encode(public).decode("ascii")


@pytest.fixture
def trust_test_key(ephemeral_key):
    """Trust ONLY the ephemeral key for the duration of a test."""
    _, public_b64 = ephemeral_key
    set_trusted_signing_keys({TEST_KEY_ID: public_b64})
    yield
    set_trusted_signing_keys(None)


@pytest.fixture
def registered_pack(monkeypatch):
    """Register a synthetic pack, returning a setter for its certification block."""

    def _register(certification, pack_id="at831_test_pack"):
        pack = {
            "packId": pack_id,
            "packVersion": "3.1.4",
            "packName": "AT-831 Test Pack",
            "domain": "service_cloud",
            "pack_domain": "service_cloud",
            "compatibility": {
                "minPlatformVersion": "1.0.0",
                "maxPlatformVersion": None,
                "requiredConcepts": [],
                "optionalConcepts": [],
            },
            "detectors": [],
            "ui_labels_path": None,
            "llm_context": "test",
        }
        if certification is not None:
            pack[CERTIFICATION_KEY] = certification
        monkeypatch.setitem(PACK_REGISTRY, pack_id, pack)
        return pack_id

    return _register


def _declaration(level=LEVEL_CERTIFIED, **overrides):
    declaration = {
        "level": level,
        "certifyingEntity": CLOUDFULCRUM,
        "reviewDate": "2026-07-31",
        "reviewedAgainstPlatformVersion": PLATFORM_VERSION,
        "scope": {
            "summary": "Detectors, evidence discipline, terminology, calibration.",
            "criteria": ["evidence_discipline", "terminology"],
        },
        "signature": {
            "keyId": TEST_KEY_ID,
            "algorithm": ALGORITHM_ED25519,
            "value": "",
        },
    }
    declaration.update(overrides)
    return declaration


def _signed(pack_id, seed, declaration):
    """Return ``declaration`` with a real signature over its canonical payload."""
    signed = copy.deepcopy(declaration)
    # The declaration must be normalised the same way at signing and verification
    # time, so sign what the accessor would produce, not the raw literal.
    signed["signature"]["value"] = sign_certification(
        pack_id, signed, seed, key_id=TEST_KEY_ID
    )
    return signed


# ── Structural: the shipped declarations stay honest ──────────────────────────


def test_every_registered_pack_declares_certification():
    for pack_id in list_packs():
        assert CERTIFICATION_KEY in PACK_REGISTRY[pack_id], (
            f"pack '{pack_id}' declares no certification block — a pack without one "
            f"reads as Community, which is probably not what was intended"
        )


def test_every_shipped_pack_declares_a_legal_level():
    for pack_id in list_packs():
        declaration = get_pack_certification_declaration(pack_id)
        assert declaration["level"] in CERTIFICATION_LEVELS


def test_every_shipped_certification_verifies_against_the_shipped_anchor():
    """AC1's positive half: a genuine badge survives verification unchanged."""
    for pack_id in list_packs():
        certification = get_pack_certification(pack_id)
        assert not certification.downgraded, (
            f"pack '{pack_id}' certification failed verification: "
            f"{certification.downgrade_detail}"
        )
        assert certification.effective_level == certification.declared_level
        assert certification.signature_verified is True


def test_shipped_certified_packs_name_cloudfulcrum_and_a_review_date():
    for pack_id in list_packs():
        declaration = get_pack_certification_declaration(pack_id)
        if declaration["level"] not in SIGNATURE_REQUIRED_LEVELS:
            continue
        assert declaration["certifyingEntity"] == CLOUDFULCRUM
        assert declaration["reviewDate"]
        assert declaration["reviewedAgainstPlatformVersion"]
        assert declaration["scope"]["summary"]
        assert declaration["scope"]["criteria"]


def test_no_shipped_pack_is_review_due_on_the_current_platform():
    for pack_id in list_packs():
        certification = get_pack_certification(pack_id)
        assert certification.review_due is False, (
            f"pack '{pack_id}' was certified against platform "
            f"{certification.reviewed_against_platform_version} but this platform is "
            f"{PLATFORM_VERSION} — re-review and re-issue its certification"
        )


def test_only_public_keys_ship():
    """An Ed25519 PUBLIC key is 32 bytes; a private seed is also 32 bytes, so this
    checks the shape rather than the secrecy — the real guarantee is that signing
    lives in scripts/, never in the verification path."""
    for key_id, material in CLOUDFULCRUM_SIGNING_KEYS.items():
        assert len(base64.b64decode(material, validate=True)) == 32, key_id
    assert "sign" not in dir(pack_certification.SignatureVerification)


# ── Declaration normalisation ─────────────────────────────────────────────────


def test_undeclared_pack_reads_as_community(registered_pack):
    pack_id = registered_pack(None)
    declaration = get_pack_certification_declaration(pack_id)
    assert declaration["level"] == LEVEL_COMMUNITY
    assert declaration["signature"]["value"] == ""

    certification = get_pack_certification(pack_id)
    assert certification.effective_level == LEVEL_COMMUNITY
    assert certification.downgraded is False  # community is not a downgrade
    assert certification.label == "Community"


def test_partial_declaration_is_filled_not_rejected(registered_pack):
    pack_id = registered_pack({"level": "PARTNER"})
    declaration = get_pack_certification_declaration(pack_id)
    assert declaration["level"] == LEVEL_PARTNER          # lower-cased
    assert declaration["certifyingEntity"] == ""          # never invented
    assert declaration["scope"] == {"summary": "", "criteria": []}


def test_scope_criteria_are_deduplicated_order_preservingly(registered_pack):
    pack_id = registered_pack(
        {
            "level": LEVEL_COMMUNITY,
            "scope": {"criteria": ["b", "a", "b", "  ", 7, "a"]},
        }
    )
    assert get_pack_certification_declaration(pack_id)["scope"]["criteria"] == [
        "b",
        "a",
    ]


def test_unknown_pack_id_reads_the_default_packs_certification():
    assert get_pack_certification("no_such_pack").pack_id == DEFAULT_PACK


# ── Canonical payload ─────────────────────────────────────────────────────────


def test_canonical_payload_is_deterministic_and_key_ordered():
    declaration = _declaration()
    first = canonical_payload_bytes(certification_payload("p", declaration))
    reordered = {k: declaration[k] for k in reversed(list(declaration))}
    second = canonical_payload_bytes(certification_payload("p", reordered))
    assert first == second
    assert json.loads(first)["payloadVersion"] == SIGNATURE_PAYLOAD_VERSION


def test_payload_covers_every_reader_facing_field_but_not_the_signature():
    payload = certification_payload("p", _declaration())
    assert set(payload) == {
        "payloadVersion",
        "packId",
        "level",
        "certifyingEntity",
        "reviewDate",
        "reviewedAgainstPlatformVersion",
        "scope",
    }
    assert "signature" not in payload


def test_payload_does_not_bind_the_pack_version(registered_pack, ephemeral_key,
                                                trust_test_key, monkeypatch):
    """A patch bump must not invalidate a certification — otherwise routine
    maintenance becomes a re-issuance ceremony and the check gets disabled."""
    seed, _ = ephemeral_key
    pack_id = registered_pack(None)
    PACK_REGISTRY[pack_id][CERTIFICATION_KEY] = _signed(
        pack_id, seed, _declaration()
    )
    assert get_pack_certification(pack_id).signature_verified is True

    PACK_REGISTRY[pack_id]["packVersion"] = "9.9.9"
    certification = get_pack_certification(pack_id)
    assert certification.signature_verified is True
    assert certification.pack_version == "9.9.9"


# ── Signature verification: AC1 ───────────────────────────────────────────────


def test_signed_certification_round_trips(registered_pack, ephemeral_key,
                                          trust_test_key):
    seed, _ = ephemeral_key
    pack_id = registered_pack(None)
    PACK_REGISTRY[pack_id][CERTIFICATION_KEY] = _signed(
        pack_id, seed, _declaration()
    )

    certification = get_pack_certification(pack_id)
    assert certification.effective_level == LEVEL_CERTIFIED
    assert certification.label == LEVEL_LABELS[LEVEL_CERTIFIED]
    assert certification.downgrade_reason is None
    assert CLOUDFULCRUM in certification.summary


def test_certified_claim_without_a_signature_is_treated_as_community(
    registered_pack,
):
    """AC1 — the self-applied-label case."""
    pack_id = registered_pack(_declaration())  # signature value left empty

    certification = get_pack_certification(pack_id)
    assert certification.declared_level == LEVEL_CERTIFIED
    assert certification.effective_level == LEVEL_COMMUNITY
    assert certification.downgraded is True
    assert certification.downgrade_reason == REASON_SIGNATURE_MISSING
    assert certification.label == LEVEL_LABELS[LEVEL_COMMUNITY]
    assert "could not be verified" in certification.summary


def test_partner_claim_without_a_signature_is_treated_as_community(
    registered_pack,
):
    pack_id = registered_pack(
        _declaration(level=LEVEL_PARTNER, certifyingEntity="Some Partner Ltd")
    )
    certification = get_pack_certification(pack_id)
    assert certification.effective_level == LEVEL_COMMUNITY
    assert certification.downgrade_reason == REASON_SIGNATURE_MISSING


@pytest.mark.parametrize(
    "field, value",
    [
        ("level", LEVEL_CERTIFIED),
        ("certifyingEntity", CLOUDFULCRUM),
        ("reviewDate", "2020-01-01"),
        ("reviewedAgainstPlatformVersion", "1.0.0"),
    ],
)
def test_editing_any_signed_field_invalidates_the_signature(
    registered_pack, ephemeral_key, trust_test_key, field, value
):
    seed, _ = ephemeral_key
    pack_id = registered_pack(None)
    # Sign at PARTNER, then try to upgrade the claim (or rewrite provenance).
    declaration = _signed(
        pack_id,
        seed,
        _declaration(level=LEVEL_PARTNER, certifyingEntity="Some Partner Ltd"),
    )
    declaration[field] = value
    PACK_REGISTRY[pack_id][CERTIFICATION_KEY] = declaration

    certification = get_pack_certification(pack_id)
    assert certification.effective_level == LEVEL_COMMUNITY
    assert certification.downgraded is True
    assert certification.downgrade_reason in {
        REASON_SIGNATURE_INVALID,
        REASON_MISSING_METADATA,
    }


def test_editing_the_scope_invalidates_the_signature(
    registered_pack, ephemeral_key, trust_test_key
):
    seed, _ = ephemeral_key
    pack_id = registered_pack(None)
    declaration = _signed(pack_id, seed, _declaration())
    declaration["scope"]["criteria"].append("aggregation_floor")
    PACK_REGISTRY[pack_id][CERTIFICATION_KEY] = declaration

    certification = get_pack_certification(pack_id)
    assert certification.downgrade_reason == REASON_SIGNATURE_INVALID
    assert certification.effective_level == LEVEL_COMMUNITY


def test_a_signature_for_another_pack_does_not_transfer(
    registered_pack, ephemeral_key, trust_test_key
):
    """The pack id is inside the payload, so a valid badge cannot be copied across."""
    seed, _ = ephemeral_key
    signed = _signed("some_other_pack", seed, _declaration())
    pack_id = registered_pack(signed)

    assert get_pack_certification(pack_id).downgrade_reason == (
        REASON_SIGNATURE_INVALID
    )


def test_signature_from_an_untrusted_key_is_rejected(
    registered_pack, ephemeral_key
):
    """No trust anchor override — the ephemeral key is not a CloudFulcrum key."""
    seed, _ = ephemeral_key
    pack_id = registered_pack(None)
    PACK_REGISTRY[pack_id][CERTIFICATION_KEY] = _signed(
        pack_id, seed, _declaration()
    )

    certification = get_pack_certification(pack_id)
    assert certification.downgrade_reason == REASON_SIGNATURE_UNKNOWN_KEY
    assert certification.effective_level == LEVEL_COMMUNITY


def test_unsupported_algorithm_is_rejected(registered_pack, ephemeral_key,
                                           trust_test_key):
    seed, _ = ephemeral_key
    pack_id = registered_pack(None)
    declaration = _signed(pack_id, seed, _declaration())
    declaration["signature"]["algorithm"] = "hmac-sha256"
    PACK_REGISTRY[pack_id][CERTIFICATION_KEY] = declaration

    assert get_pack_certification(pack_id).downgrade_reason == (
        REASON_SIGNATURE_UNSUPPORTED_ALGORITHM
    )


def test_malformed_signature_value_is_rejected(registered_pack, trust_test_key):
    declaration = _declaration()
    declaration["signature"]["value"] = "not base64!!"
    pack_id = registered_pack(declaration)

    assert get_pack_certification(pack_id).downgrade_reason == (
        REASON_SIGNATURE_MALFORMED
    )


def test_certified_level_requires_cloudfulcrum_as_the_certifying_entity(
    registered_pack, ephemeral_key, trust_test_key
):
    seed, _ = ephemeral_key
    pack_id = registered_pack(None)
    PACK_REGISTRY[pack_id][CERTIFICATION_KEY] = _signed(
        pack_id, seed, _declaration(certifyingEntity="Partner Self Ltd")
    )

    certification = get_pack_certification(pack_id)
    assert certification.downgrade_reason == REASON_MISSING_METADATA
    assert certification.effective_level == LEVEL_COMMUNITY


def test_missing_metadata_is_named(registered_pack, trust_test_key):
    pack_id = registered_pack(_declaration(reviewDate="", certifyingEntity=""))
    certification = get_pack_certification(pack_id)
    assert certification.downgrade_reason == REASON_MISSING_METADATA
    assert "certifyingEntity" in certification.downgrade_detail
    assert "reviewDate" in certification.downgrade_detail


def test_illegal_level_is_treated_as_community(registered_pack):
    pack_id = registered_pack(_declaration(level="platinum"))
    certification = get_pack_certification(pack_id)
    assert certification.downgrade_reason == REASON_INVALID_LEVEL
    assert certification.effective_level == LEVEL_COMMUNITY


def test_community_pack_needs_no_signature(registered_pack):
    pack_id = registered_pack({"level": LEVEL_COMMUNITY})
    certification = get_pack_certification(pack_id)
    assert certification.effective_level == LEVEL_COMMUNITY
    assert certification.downgraded is False
    assert certification.signature_verified is False
    assert "self-declared" in certification.summary


def test_verify_returns_a_verdict_and_never_raises():
    verdict = verify_certification_signature("p", {"signature": {"value": "??"}})
    assert verdict.verified is False
    assert verdict.reason is not None


# ── Trust anchors ─────────────────────────────────────────────────────────────


def test_environment_may_add_a_trust_anchor(monkeypatch, ephemeral_key):
    _, public_b64 = ephemeral_key
    monkeypatch.setenv(
        TRUSTED_KEYS_ENV_VAR, json.dumps({"partner-key": public_b64})
    )
    assert "partner-key" in trusted_key_ids()
    assert set(CLOUDFULCRUM_SIGNING_KEYS).issubset(set(trusted_key_ids()))


def test_environment_cannot_override_a_builtin_anchor(monkeypatch, ephemeral_key):
    _, public_b64 = ephemeral_key
    builtin = next(iter(CLOUDFULCRUM_SIGNING_KEYS))
    monkeypatch.setenv(TRUSTED_KEYS_ENV_VAR, json.dumps({builtin: public_b64}))
    assert trusted_signing_keys()[builtin] == CLOUDFULCRUM_SIGNING_KEYS[builtin]


def test_malformed_trust_anchor_env_is_ignored(monkeypatch):
    monkeypatch.setenv(TRUSTED_KEYS_ENV_VAR, "{not json")
    assert trusted_signing_keys() == dict(CLOUDFULCRUM_SIGNING_KEYS)


# ── Levels, selection, and the summary ────────────────────────────────────────


def test_level_ordering_supports_a_certified_only_policy():
    assert meets_minimum_level(LEVEL_CERTIFIED, LEVEL_CERTIFIED)
    assert not meets_minimum_level(LEVEL_PARTNER, LEVEL_CERTIFIED)
    assert not meets_minimum_level(LEVEL_COMMUNITY, LEVEL_PARTNER)
    assert meets_minimum_level(LEVEL_PARTNER, LEVEL_COMMUNITY)
    # Fail closed on an unrecognised level.
    assert not meets_minimum_level("platinum", LEVEL_PARTNER)


def test_get_certification_level_reports_the_effective_level(registered_pack):
    pack_id = registered_pack(_declaration())  # unsigned certified claim
    assert get_certification_level(pack_id) == LEVEL_COMMUNITY


def test_selection_is_order_preserving_and_deduplicated():
    reports = certify_pack_selection(["cloud_ops", "ncino", "cloud_ops"])
    assert [report.pack_id for report in reports] == ["cloud_ops", "ncino"]


def test_empty_selection_reports_the_default_pack():
    assert [r.pack_id for r in certify_pack_selection([])] == [DEFAULT_PACK]


def test_summary_shape():
    summary = certification_summary(["cloud_ops", "security_ops"])
    assert summary["platformVersion"] == PLATFORM_VERSION
    assert summary["allVerified"] is True
    assert summary["reviewDue"] == []
    assert summary["trustedKeyIds"] == trusted_key_ids()
    assert [pack["packId"] for pack in summary["packs"]] == [
        "cloud_ops",
        "security_ops",
    ]
    pack = summary["packs"][0]
    for key in (
        "declaredLevel",
        "level",
        "levelLabel",
        "certifyingEntity",
        "reviewDate",
        "reviewedAgainstPlatformVersion",
        "scope",
        "signatureVerified",
        "downgraded",
        "reviewDue",
        "summary",
    ):
        assert key in pack


def test_summary_reports_an_unverified_claim(registered_pack):
    pack_id = registered_pack(_declaration())
    summary = certification_summary([pack_id])
    assert summary["allVerified"] is False
    assert summary["packs"][0]["downgraded"] is True
    assert summary["packs"][0]["level"] == LEVEL_COMMUNITY
    assert summary["packs"][0]["declaredLevel"] == LEVEL_CERTIFIED


# ── Review due ────────────────────────────────────────────────────────────────


def test_platform_minor_bump_makes_a_certification_review_due():
    certification = get_pack_certification("cloud_ops", platform_version="2.1.0")
    assert certification.review_due is True
    assert certification.review_due_reason == REVIEW_DUE_PLATFORM_MOVED
    # The badge is NOT lost — it is flagged. The signature is still valid.
    assert certification.effective_level == LEVEL_CERTIFIED
    assert certification.signature_verified is True
    assert "review due" in certification.status_label


def test_platform_patch_bump_does_not_make_a_certification_review_due():
    assert (
        get_pack_certification("cloud_ops", platform_version="2.0.9").review_due
        is False
    )


def test_community_pack_is_never_review_due(registered_pack):
    pack_id = registered_pack({"level": LEVEL_COMMUNITY})
    assert (
        get_pack_certification(pack_id, platform_version="9.9.9").review_due is False
    )


def test_unverified_claim_is_not_reported_as_review_due(registered_pack):
    """A downgraded pack is Community, and Community was never reviewed — reporting
    it as 'review due' would imply a badge it does not have."""
    pack_id = registered_pack(_declaration())
    assert (
        get_pack_certification(pack_id, platform_version="9.9.9").review_due is False
    )
