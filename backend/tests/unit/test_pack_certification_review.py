"""2.0-C2 T2 (AT-832) — the certification review workflow, DB-free.

Sub-task scope: *a checklist-driven review recording who reviewed what and against
which criteria — declarative-manifest review, evidence-discipline conformance,
terminology, and calibration sanity.*

Parent-story criterion discharged here:

  * AC5 — every certification decision is recorded with reviewer, criteria, and
    date, and is auditable.

The API surface, RBAC, and org isolation are pinned over HTTP in
``tests/contract/test_pack_certification_review_api.py``; this file pins the state
machine, the checklist gate, the append-only trail, and the review→signature bridge.

Also pinned: the checklist vocabulary and the shipped packs' signed
``scope.criteria`` cannot drift apart — the same ids are read from both ends.
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("INGEST_MODE", "offline")

from app.history_retention import PROTECTED_TABLES  # noqa: E402
from app.pack_certification_review import (  # noqa: E402
    DECISION_APPROVED,
    DECISION_REJECTED,
    InMemoryPackCertificationReviewStore,
    OUTCOME_FAIL,
    OUTCOME_NOT_APPLICABLE,
    OUTCOME_PASS,
    PackCertificationReview,
    ReviewDecisionError,
    ReviewValidationError,
    latest_review,
    latest_reviews_safe,
    normalize_criteria,
    pack_review_history,
    record_certification_review,
    review_view,
    set_review_store,
)
from app.pack_state import PackNotFound  # noqa: E402
from discovery.packs.certification_criteria import (  # noqa: E402
    CERTIFICATION_CRITERIA,
    REQUIRED_CRITERION_IDS,
    criteria_catalog,
    describe_criterion,
    get_criterion,
    is_known_criterion,
)
from discovery.packs.pack_certification import (  # noqa: E402
    CLOUDFULCRUM,
    LEVEL_CERTIFIED,
    LEVEL_COMMUNITY,
    LEVEL_PARTNER,
    canonical_payload_bytes,
    certification_payload,
    set_trusted_signing_keys,
    sign_certification,
)
from discovery.packs.pack_config import (  # noqa: E402
    get_pack_certification_declaration,
    get_pack_version,
    list_packs,
)
from discovery.packs.platform_capabilities import PLATFORM_VERSION  # noqa: E402

ORG = "org_at832"
PACK = "cloud_ops"


@pytest.fixture(autouse=True)
def in_memory_store():
    set_review_store(InMemoryPackCertificationReviewStore())
    yield
    set_review_store(None)


def _all_pass(*, extra=()):
    results = [
        {"criterionId": cid, "outcome": OUTCOME_PASS} for cid in REQUIRED_CRITERION_IDS
    ]
    results.extend(extra)
    return results


def _record(**overrides):
    kwargs = {
        "reviewer_id": "reviewer@cloudfulcrum.com",
        "proposed_level": LEVEL_CERTIFIED,
        "decision": DECISION_APPROVED,
        "criteria": _all_pass(),
        "scope_summary": "Detectors, evidence discipline, terminology, calibration.",
    }
    kwargs.update(overrides)
    org = kwargs.pop("org_id", ORG)
    pack = kwargs.pop("pack_id", PACK)
    return record_certification_review(org, pack, **kwargs)


# ── The checklist vocabulary ──────────────────────────────────────────────────


def test_the_story_named_four_required_criteria():
    """The four the story names are required; nothing silently became optional."""
    assert REQUIRED_CRITERION_IDS == [
        "declarative_manifest_review",
        "evidence_discipline",
        "terminology",
        "calibration_sanity",
    ]


def test_every_criterion_has_a_label_and_description():
    for criterion_id, spec in CERTIFICATION_CRITERIA.items():
        assert spec.criterion_id == criterion_id
        assert spec.label.strip()
        assert spec.description.strip()


def test_catalog_is_serialisable_and_ordered():
    catalog = criteria_catalog()
    assert [item["criterionId"] for item in catalog] == list(CERTIFICATION_CRITERIA)
    assert catalog[0]["required"] is True


def test_unknown_criterion_lookups_are_safe():
    assert get_criterion("nope") is None
    assert is_known_criterion("nope") is False
    assert describe_criterion("nope") == "nope"


def test_shipped_pack_scopes_only_claim_real_criteria():
    """AT-831's signed ``scope.criteria`` and this checklist are one vocabulary.

    A pack claiming a criterion that does not exist would put an unreviewable item
    inside a signature.
    """
    for pack_id in list_packs():
        declared = get_pack_certification_declaration(pack_id)
        for criterion_id in declared["scope"]["criteria"]:
            assert is_known_criterion(criterion_id), (
                f"pack '{pack_id}' claims unknown review criterion "
                f"{criterion_id!r} in its signed certification scope"
            )


def test_every_certified_pack_claims_all_required_criteria():
    for pack_id in list_packs():
        declared = get_pack_certification_declaration(pack_id)
        if declared["level"] == LEVEL_COMMUNITY:
            continue
        missing = set(REQUIRED_CRITERION_IDS) - set(declared["scope"]["criteria"])
        assert not missing, (
            f"pack '{pack_id}' is certified but its scope omits required criteria: "
            f"{sorted(missing)}"
        )


# ── Recording a review ────────────────────────────────────────────────────────


def test_review_records_reviewer_criteria_and_date():
    """AC5, directly."""
    review = _record()
    assert review.reviewer_id == "reviewer@cloudfulcrum.com"
    assert [result.criterion_id for result in review.criteria] == REQUIRED_CRITERION_IDS
    assert review.reviewed_at  # ISO timestamp, server-stamped
    assert review.review_id.startswith("pcr_")
    assert review.revision == 1
    assert review.approved is True


def test_pack_and_platform_versions_are_stamped_server_side():
    review = _record()
    assert review.pack_version == get_pack_version(PACK)
    assert review.platform_version == PLATFORM_VERSION


def test_review_is_appended_not_overwritten():
    first = _record()
    second = _record(decision=DECISION_REJECTED, notes="regression in queue ageing",
                     criteria=_all_pass())
    history = pack_review_history(ORG, PACK)
    assert [r.revision for r in history] == [2, 1]        # newest first
    assert history[0].review_id == second.review_id
    assert history[1].review_id == first.review_id        # the earlier one survives


def test_latest_review_is_the_newest():
    _record()
    latest = _record(reviewer_name="Second Reviewer")
    assert latest_review(ORG, PACK).review_id == latest.review_id


def test_no_review_yet_reads_as_none():
    assert latest_review(ORG, PACK) is None
    assert pack_review_history(ORG, PACK) == []


def test_reviews_are_org_scoped():
    _record(org_id="org_a")
    assert pack_review_history("org_b", PACK) == []
    assert latest_reviews_safe("org_a")[PACK].org_id == "org_a"
    assert latest_reviews_safe("org_b") == {}


def test_latest_reviews_safe_degrades_on_an_unreadable_store():
    class Broken(InMemoryPackCertificationReviewStore):
        def latest_per_pack(self, org_id):
            raise RuntimeError("store down")

    set_review_store(Broken())
    assert latest_reviews_safe(ORG) == {}


def test_unknown_pack_is_refused_not_resolved_to_the_default():
    """A typo must not put a real reviewer's name against a pack they never saw."""
    with pytest.raises(PackNotFound):
        _record(pack_id="no_such_pack")


# ── The checklist gate ────────────────────────────────────────────────────────


def test_approval_requires_every_required_criterion():
    partial = [
        {"criterionId": cid, "outcome": OUTCOME_PASS}
        for cid in REQUIRED_CRITERION_IDS[:2]
    ]
    with pytest.raises(ReviewDecisionError) as exc:
        _record(criteria=partial)
    for cid in REQUIRED_CRITERION_IDS[2:]:
        assert cid in str(exc.value)


def test_approval_is_refused_when_a_required_criterion_failed():
    criteria = _all_pass()
    criteria[1] = {"criterionId": criteria[1]["criterionId"], "outcome": OUTCOME_FAIL,
                   "note": "confidence inflated on single-source findings"}
    with pytest.raises(ReviewDecisionError) as exc:
        _record(criteria=criteria)
    assert "evidence_discipline" in str(exc.value)
    assert "fail" in str(exc.value)


def test_approval_is_refused_when_a_required_criterion_is_not_applicable():
    criteria = _all_pass()
    criteria[3] = {
        "criterionId": criteria[3]["criterionId"],
        "outcome": OUTCOME_NOT_APPLICABLE,
        "note": "no scorer in this pack",
    }
    with pytest.raises(ReviewDecisionError):
        _record(criteria=criteria)


def test_rejection_is_allowed_with_a_failed_criterion_and_a_reason():
    criteria = _all_pass()
    criteria[0] = {"criterionId": criteria[0]["criterionId"], "outcome": OUTCOME_FAIL,
                   "note": "manifest references a non-primitive detector"}
    review = _record(
        criteria=criteria, decision=DECISION_REJECTED, notes="see criterion notes"
    )
    assert review.approved is False
    assert review.decision == DECISION_REJECTED


def test_rejection_without_a_reason_is_refused():
    with pytest.raises(ReviewDecisionError) as exc:
        _record(decision=DECISION_REJECTED, notes=None)
    assert "why" in str(exc.value)


def test_optional_criterion_may_be_not_applicable_with_a_note():
    review = _record(
        criteria=_all_pass(
            extra=[
                {
                    "criterionId": "aggregation_floor",
                    "outcome": OUTCOME_NOT_APPLICABLE,
                    "note": "pack surfaces no security-derived content",
                }
            ]
        )
    )
    assert review.approved is True
    assert "aggregation_floor" not in review.criteria_ids  # not a pass, not claimed


# ── Checklist validation ──────────────────────────────────────────────────────


def test_unknown_criterion_is_rejected():
    with pytest.raises(ReviewValidationError) as exc:
        normalize_criteria([{"criterionId": "vibes", "outcome": OUTCOME_PASS}])
    assert "vibes" in str(exc.value)


def test_duplicate_verdict_is_rejected():
    with pytest.raises(ReviewValidationError) as exc:
        normalize_criteria(
            [
                {"criterionId": "terminology", "outcome": OUTCOME_PASS},
                {"criterionId": "terminology", "outcome": OUTCOME_FAIL},
            ]
        )
    assert "more than one verdict" in str(exc.value)


def test_unknown_outcome_is_rejected():
    with pytest.raises(ReviewValidationError):
        normalize_criteria([{"criterionId": "terminology", "outcome": "probably"}])


def test_not_applicable_without_a_note_is_rejected():
    with pytest.raises(ReviewValidationError) as exc:
        normalize_criteria(
            [{"criterionId": "terminology", "outcome": OUTCOME_NOT_APPLICABLE}]
        )
    assert "not_applicable" in str(exc.value)


def test_empty_checklist_is_rejected():
    with pytest.raises(ReviewValidationError):
        normalize_criteria([])


def test_community_cannot_be_reviewed():
    """Community is self-declared — a review granting it records no decision."""
    with pytest.raises(ReviewValidationError) as exc:
        _record(proposed_level=LEVEL_COMMUNITY)
    assert "certified" in str(exc.value).lower()


def test_unknown_decision_is_rejected():
    with pytest.raises(ReviewValidationError):
        _record(decision="maybe")


def test_scope_summary_is_required():
    with pytest.raises(ReviewValidationError):
        _record(scope_summary="   ")


def test_reviewer_id_is_required():
    with pytest.raises(ReviewValidationError):
        _record(reviewer_id="")


# ── The review → signature bridge ─────────────────────────────────────────────


def test_approved_review_emits_the_declaration_to_be_signed():
    review = _record(scope_summary="Cloud-ops detectors and calibration.")
    declaration = review.approved_declaration()
    assert declaration["level"] == LEVEL_CERTIFIED
    assert declaration["certifyingEntity"] == CLOUDFULCRUM
    assert declaration["reviewDate"] == review.reviewed_at[:10]
    assert declaration["reviewedAgainstPlatformVersion"] == PLATFORM_VERSION
    assert declaration["scope"]["criteria"] == REQUIRED_CRITERION_IDS
    # The review cannot mint a signature — the slot is present and empty.
    assert declaration["signature"] == {"keyId": "", "algorithm": "", "value": ""}


def test_partner_level_review_declares_partner():
    review = _record(proposed_level=LEVEL_PARTNER)
    assert review.approved_declaration()["level"] == LEVEL_PARTNER


def test_canonical_payload_is_what_the_key_holder_signs():
    review = _record()
    declaration = review.approved_declaration()
    expected = canonical_payload_bytes(
        certification_payload(PACK, declaration)
    ).decode("utf-8")
    assert review.canonical_payload() == expected


def test_the_signed_review_verifies_end_to_end(monkeypatch):
    """What is approved is exactly what verifies — the two tasks meet here."""
    import base64

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from discovery.packs import pack_config
    from discovery.packs.pack_certification import get_pack_certification

    private_key = Ed25519PrivateKey.generate()
    seed = private_key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public = base64.b64encode(
        private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
    ).decode("ascii")

    review = _record()
    declaration = review.approved_declaration()
    declaration["signature"] = {
        "keyId": "review-key",
        "algorithm": "ed25519",
        "value": sign_certification(PACK, declaration, seed, key_id="review-key"),
    }

    set_trusted_signing_keys({"review-key": public})
    monkeypatch.setitem(
        pack_config.PACK_REGISTRY[PACK], "certification", declaration
    )
    try:
        certification = get_pack_certification(PACK)
        assert certification.signature_verified is True
        assert certification.effective_level == LEVEL_CERTIFIED
        assert certification.review_date == review.reviewed_at[:10]
    finally:
        set_trusted_signing_keys(None)


def test_rejected_review_has_nothing_to_sign():
    review = _record(decision=DECISION_REJECTED, notes="not ready")
    with pytest.raises(ReviewDecisionError):
        review.approved_declaration()
    with pytest.raises(ReviewDecisionError):
        review.canonical_payload()


def test_declaration_claims_only_criteria_that_passed():
    review = _record(
        criteria=_all_pass(
            extra=[
                {"criterionId": "compliance_guardrails", "outcome": OUTCOME_PASS},
                {
                    "criterionId": "aggregation_floor",
                    "outcome": OUTCOME_NOT_APPLICABLE,
                    "note": "no security content",
                },
            ]
        )
    )
    criteria = review.approved_declaration()["scope"]["criteria"]
    assert "compliance_guardrails" in criteria
    assert "aggregation_floor" not in criteria


# ── Serialisation and the combined view ───────────────────────────────────────


def test_as_dict_carries_the_audit_fields():
    review = _record(reviewer_name="A Reviewer")
    payload = review.as_dict()
    for key in (
        "reviewId",
        "packId",
        "packVersion",
        "revision",
        "reviewerId",
        "reviewerName",
        "reviewedAt",
        "reviewedAgainstPlatformVersion",
        "proposedLevel",
        "decision",
        "criteria",
        "scopeSummary",
        "summary",
    ):
        assert key in payload
    assert payload["certificationDeclaration"]["level"] == LEVEL_CERTIFIED
    assert payload["canonicalPayload"]


def test_rejected_review_dict_carries_no_signing_material():
    payload = _record(decision=DECISION_REJECTED, notes="not ready").as_dict()
    assert "certificationDeclaration" not in payload
    assert "canonicalPayload" not in payload


def test_review_view_reports_the_badge_and_the_trail_separately():
    """An approved-but-unsigned pack must not read as Certified."""
    review = _record()
    view = review_view(ORG, PACK)
    assert view["latestReview"]["reviewId"] == review.review_id
    assert len(view["reviews"]) == 1
    # The badge still comes from the SIGNATURE, not from this approval.
    assert view["certification"]["packId"] == PACK
    assert "level" in view["certification"]


def test_summary_names_reviewer_pack_and_date():
    review = _record(reviewer_name="A Reviewer")
    assert "A Reviewer" in review.summary
    assert PACK in review.summary
    assert review.reviewed_at[:10] in review.summary


# ── Auditability ──────────────────────────────────────────────────────────────


def test_the_review_trail_is_protected_history():
    assert "pack_certification_reviews" in PROTECTED_TABLES


def test_the_store_contract_has_no_delete_or_update():
    from app.pack_certification_review import PackCertificationReviewStore

    surface = {
        name for name in dir(PackCertificationReviewStore) if not name.startswith("_")
    }
    assert not {
        name
        for name in surface
        if any(verb in name for verb in ("delete", "remove", "update", "purge"))
    }


def test_module_contains_no_destructive_sql():
    from pathlib import Path

    source = Path(
        __file__
    ).resolve().parents[2] / "app" / "pack_certification_review.py"
    text = source.read_text(encoding="utf-8").upper()
    assert "DELETE FROM" not in text
    assert "TRUNCATE" not in text
    assert "UPDATE PACK_CERTIFICATION_REVIEWS" not in text


def test_review_is_immutable():
    review = _record()
    with pytest.raises(Exception):
        review.decision = DECISION_REJECTED  # type: ignore[misc]
    assert isinstance(review, PackCertificationReview)
