"""2.0-C2 T2 (AT-832) — the certification review workflow over HTTP.

Parent-story criterion exercised here:

  * **AC5** — every certification decision is recorded with reviewer, criteria, and
    date, and is auditable (the domain trail AND the organisation-wide audit log).

Plus the properties that make the workflow trustworthy rather than decorative: the
RBAC boundary, org isolation, server-side attribution (the caller cannot supply the
reviewer, the pack version, or the date), the checklist gate over the wire, and the
fact that recording an approval does NOT change a pack's effective level — only the
AT-831 signature does.

The state machine itself is pinned DB-free in
``tests/unit/test_pack_certification_review.py``.
"""
from __future__ import annotations

import os
from typing import Any, Dict, Iterator, List
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.pack_certification_review import (
    InMemoryPackCertificationReviewStore,
    set_review_store,
)
from app.rbac import seed_owner
from discovery.packs.certification_criteria import REQUIRED_CRITERION_IDS
from discovery.packs.pack_certification import LEVEL_COMMUNITY
from discovery.packs.pack_config import get_pack_version
from discovery.packs.platform_capabilities import PLATFORM_VERSION

OWNER_TOKEN = os.getenv("DEV_JWT", "dev-token-change-me")
ANALYST_TOKEN = "analyst-token"
VIEWER_TOKEN = "viewer-token"

PACK = "cloud_ops"

_CURRENT_ORG: Dict[str, Any] = {"id": None}


@pytest.fixture(autouse=True)
def _role_tokens(monkeypatch):
    monkeypatch.setenv("ANALYST_JWT", ANALYST_TOKEN)
    monkeypatch.setenv("VIEWER_JWT", VIEWER_TOKEN)
    yield


@pytest.fixture(autouse=True)
def _in_memory_reviews():
    """Isolate the review trail per test, and restore the Postgres store after."""
    set_review_store(InMemoryPackCertificationReviewStore())
    yield
    set_review_store(None)


@pytest.fixture
def isolated_org() -> Iterator[str]:
    """A throwaway org with the dev token seeded as owner.

    Reviews are org-scoped and never deleted, so a shared org would let one test's
    trail leak into another's assertions on ordering.
    """
    previous = _CURRENT_ORG["id"]
    org_id = f"cert_review_{uuid4().hex[:8]}"
    seed_owner(org_id, OWNER_TOKEN)
    _CURRENT_ORG["id"] = org_id
    try:
        yield org_id
    finally:
        _CURRENT_ORG["id"] = previous


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def _auth(token: str = OWNER_TOKEN, org_id: str | None = None) -> Dict[str, str]:
    headers = {"Authorization": f"Bearer {token}"}
    org = org_id or _CURRENT_ORG["id"]
    if org is not None:
        headers["X-Org-Id"] = org
    return headers


def _verdicts(**overrides: str) -> List[Dict[str, Any]]:
    results = [
        {"criterionId": cid, "outcome": overrides.get(cid, "pass")}
        for cid in REQUIRED_CRITERION_IDS
    ]
    for entry in results:
        if entry["outcome"] != "pass":
            entry["note"] = "reviewer note"
    return results


def _body(**overrides: Any) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "proposedLevel": "certified",
        "decision": "approved",
        "criteria": _verdicts(),
        "scopeSummary": "Detectors, evidence discipline, terminology, calibration.",
        "reviewerName": "A Reviewer",
    }
    body.update(overrides)
    return body


# ── The checklist ─────────────────────────────────────────────────────────────


def test_criteria_endpoint_serves_the_checklist(client):
    response = client.get("/api/packs/certification/criteria", headers=_auth())
    assert response.status_code == 200
    payload = response.json()
    assert payload["requiredCriteria"] == REQUIRED_CRITERION_IDS
    assert payload["platformVersion"] == PLATFORM_VERSION
    assert {"certified", "partner"} == set(payload["levels"])
    ids = [item["criterionId"] for item in payload["criteria"]]
    assert set(REQUIRED_CRITERION_IDS).issubset(set(ids))
    for item in payload["criteria"]:
        assert item["label"] and item["description"]


def test_a_viewer_can_read_the_checklist(client):
    """A reader who sees a Certified badge must be able to see what was checked."""
    response = client.get(
        "/api/packs/certification/criteria", headers=_auth(VIEWER_TOKEN)
    )
    assert response.status_code == 200


def test_checklist_requires_auth(client):
    assert client.get("/api/packs/certification/criteria").status_code in (401, 403)


# ── Recording a review ────────────────────────────────────────────────────────


def test_owner_records_a_review(client, isolated_org):
    response = client.post(
        f"/api/packs/{PACK}/certification/reviews",
        json=_body(),
        headers=_auth(),
    )
    assert response.status_code == 201
    review = response.json()
    assert review["packId"] == PACK
    assert review["decision"] == "approved"
    assert review["approved"] is True
    assert review["revision"] == 1
    assert review["reviewerName"] == "A Reviewer"
    assert review["passedCriteria"] == REQUIRED_CRITERION_IDS
    assert review["summary"]


def test_reviewer_pack_version_and_date_are_server_stamped(client, isolated_org):
    """A trail that lets the caller supply these is not an audit trail."""
    response = client.post(
        f"/api/packs/{PACK}/certification/reviews",
        json={
            **_body(),
            # All ignored — not part of the request model.
            "reviewerId": "somebody-else",
            "reviewedAt": "1999-01-01T00:00:00Z",
            "packVersion": "0.0.1",
            "orgId": "another-org",
        },
        headers=_auth(),
    )
    assert response.status_code == 201
    review = response.json()
    assert review["reviewerId"] != "somebody-else"
    assert not review["reviewedAt"].startswith("1999")
    assert review["packVersion"] == get_pack_version(PACK)
    assert review["orgId"] == isolated_org
    assert review["reviewedAgainstPlatformVersion"] == PLATFORM_VERSION


def test_approval_returns_the_material_to_be_signed(client, isolated_org):
    """The bridge to AT-831: what is signed is provably what was approved."""
    response = client.post(
        f"/api/packs/{PACK}/certification/reviews", json=_body(), headers=_auth()
    )
    review = response.json()
    declaration = review["certificationDeclaration"]
    assert declaration["level"] == "certified"
    assert declaration["certifyingEntity"] == "CloudFulcrum"
    assert declaration["signature"] == {"keyId": "", "algorithm": "", "value": ""}
    assert review["canonicalPayload"].startswith("{")
    assert PACK in review["canonicalPayload"]


def test_recording_an_approval_does_not_change_the_effective_level(
    client, isolated_org
):
    """Only a valid signature grants a badge — a database row never does."""
    unsigned = {
        "level": "certified",
        "certifyingEntity": "CloudFulcrum",
        "reviewDate": "2026-07-31",
        "reviewedAgainstPlatformVersion": PLATFORM_VERSION,
        "scope": {"summary": "s", "criteria": list(REQUIRED_CRITERION_IDS)},
        "signature": {"keyId": "", "algorithm": "", "value": ""},
    }
    from discovery.packs import pack_config

    original = pack_config.PACK_REGISTRY[PACK].get("certification")
    pack_config.PACK_REGISTRY[PACK]["certification"] = unsigned
    try:
        client.post(
            f"/api/packs/{PACK}/certification/reviews",
            json=_body(),
            headers=_auth(),
        )
        trail = client.get(
            f"/api/packs/{PACK}/certification/reviews", headers=_auth()
        ).json()
        assert trail["latestReview"]["approved"] is True
        # …and the pack is still Community, because nothing signed it.
        assert trail["certification"]["level"] == LEVEL_COMMUNITY
        assert trail["certification"]["declaredLevel"] == "certified"
    finally:
        if original is None:
            pack_config.PACK_REGISTRY[PACK].pop("certification", None)
        else:
            pack_config.PACK_REGISTRY[PACK]["certification"] = original


# ── The checklist gate, over the wire ─────────────────────────────────────────


def test_approval_missing_a_required_criterion_is_a_conflict(client, isolated_org):
    partial = [
        {"criterionId": cid, "outcome": "pass"} for cid in REQUIRED_CRITERION_IDS[:2]
    ]
    response = client.post(
        f"/api/packs/{PACK}/certification/reviews",
        json=_body(criteria=partial),
        headers=_auth(),
    )
    assert response.status_code == 409
    for cid in REQUIRED_CRITERION_IDS[2:]:
        assert cid in response.json()["detail"]


def test_approval_with_a_failed_criterion_is_a_conflict(client, isolated_org):
    response = client.post(
        f"/api/packs/{PACK}/certification/reviews",
        json=_body(criteria=_verdicts(terminology="fail")),
        headers=_auth(),
    )
    assert response.status_code == 409
    assert "terminology" in response.json()["detail"]


def test_rejection_without_a_reason_is_a_conflict(client, isolated_org):
    response = client.post(
        f"/api/packs/{PACK}/certification/reviews",
        json=_body(decision="rejected"),
        headers=_auth(),
    )
    assert response.status_code == 409


def test_rejection_with_a_reason_is_recorded(client, isolated_org):
    response = client.post(
        f"/api/packs/{PACK}/certification/reviews",
        json=_body(
            decision="rejected",
            criteria=_verdicts(evidence_discipline="fail"),
            notes="single-source findings labelled HIGH",
        ),
        headers=_auth(),
    )
    assert response.status_code == 201
    review = response.json()
    assert review["approved"] is False
    assert "certificationDeclaration" not in review


def test_unknown_criterion_is_a_bad_request(client, isolated_org):
    response = client.post(
        f"/api/packs/{PACK}/certification/reviews",
        json=_body(criteria=[{"criterionId": "vibes", "outcome": "pass"}]),
        headers=_auth(),
    )
    assert response.status_code == 400
    assert "vibes" in response.json()["detail"]


def test_not_applicable_without_a_note_is_a_bad_request(client, isolated_org):
    response = client.post(
        f"/api/packs/{PACK}/certification/reviews",
        json=_body(
            criteria=[
                {"criterionId": cid, "outcome": "pass"}
                for cid in REQUIRED_CRITERION_IDS[:-1]
            ]
            + [
                {
                    "criterionId": REQUIRED_CRITERION_IDS[-1],
                    "outcome": "not_applicable",
                }
            ]
        ),
        headers=_auth(),
    )
    assert response.status_code == 400


def test_community_level_is_not_a_reviewable_outcome(client, isolated_org):
    response = client.post(
        f"/api/packs/{PACK}/certification/reviews",
        json=_body(proposedLevel="community"),
        headers=_auth(),
    )
    assert response.status_code == 422  # not in the request model's enum


def test_unknown_pack_is_a_404(client, isolated_org):
    response = client.post(
        "/api/packs/no_such_pack/certification/reviews",
        json=_body(),
        headers=_auth(),
    )
    assert response.status_code == 404


# ── The trail ─────────────────────────────────────────────────────────────────


def test_trail_is_newest_first_and_append_only(client, isolated_org):
    client.post(
        f"/api/packs/{PACK}/certification/reviews", json=_body(), headers=_auth()
    )
    client.post(
        f"/api/packs/{PACK}/certification/reviews",
        json=_body(
            decision="rejected",
            criteria=_verdicts(calibration_sanity="fail"),
            notes="queue-ageing threshold never fires",
        ),
        headers=_auth(),
    )

    response = client.get(
        f"/api/packs/{PACK}/certification/reviews", headers=_auth()
    )
    assert response.status_code == 200
    trail = response.json()
    assert [review["revision"] for review in trail["reviews"]] == [2, 1]
    # The earlier approval is still on the trail — a later rejection does not erase it.
    assert trail["reviews"][1]["decision"] == "approved"
    assert trail["latestReview"]["decision"] == "rejected"


def test_trail_reports_the_live_badge_alongside_the_reviews(client, isolated_org):
    response = client.get(
        f"/api/packs/{PACK}/certification/reviews", headers=_auth()
    )
    assert response.status_code == 200
    certification = response.json()["certification"]
    assert certification["packId"] == PACK
    assert "level" in certification and "declaredLevel" in certification


def test_never_reviewed_pack_reports_an_empty_trail(client, isolated_org):
    response = client.get(
        f"/api/packs/{PACK}/certification/reviews", headers=_auth()
    )
    assert response.json()["reviews"] == []
    assert response.json()["latestReview"] is None


def test_trail_for_an_unknown_pack_is_a_404(client, isolated_org):
    response = client.get(
        "/api/packs/no_such_pack/certification/reviews",
        headers=_auth(),
    )
    assert response.status_code == 404


# ── RBAC and isolation ────────────────────────────────────────────────────────


def test_analyst_cannot_record_a_review(client):
    response = client.post(
        f"/api/packs/{PACK}/certification/reviews",
        json=_body(),
        headers=_auth(ANALYST_TOKEN),
    )
    assert response.status_code == 403


def test_viewer_cannot_read_the_trail(client):
    response = client.get(
        f"/api/packs/{PACK}/certification/reviews", headers=_auth(VIEWER_TOKEN)
    )
    assert response.status_code == 403


def test_review_requires_auth(client):
    response = client.post(
        f"/api/packs/{PACK}/certification/reviews", json=_body()
    )
    assert response.status_code in (401, 403)


def test_one_orgs_reviews_are_invisible_to_another(client):
    org_a = f"cert_a_{uuid4().hex[:8]}"
    org_b = f"cert_b_{uuid4().hex[:8]}"
    seed_owner(org_a, OWNER_TOKEN)
    seed_owner(org_b, OWNER_TOKEN)

    created = client.post(
        f"/api/packs/{PACK}/certification/reviews",
        json=_body(),
        headers=_auth(org_id=org_a),
    )
    assert created.status_code == 201

    trail_b = client.get(
        f"/api/packs/{PACK}/certification/reviews",
        headers=_auth(org_id=org_b),
    )
    assert trail_b.json()["reviews"] == []


# ── Auditability ──────────────────────────────────────────────────────────────


def test_the_decision_reaches_the_organisation_audit_log(client, isolated_org):
    """AC5's second half: auditable from the org-wide stream, not only the domain
    table — that is what an auditor actually reads."""
    client.post(
        f"/api/packs/{PACK}/certification/reviews", json=_body(), headers=_auth()
    )
    entries = client.get("/api/audit-log", headers=_auth()).json()
    reviewed = [
        entry
        for entry in entries
        if entry.get("event_type") == "pack_certification_reviewed"
    ]
    assert reviewed, "no pack_certification_reviewed audit entry was written"
    payload = reviewed[0].get("payload") or {}
    assert payload.get("pack_id") == PACK
    assert payload.get("decision") == "approved"
    assert payload.get("criteria") == REQUIRED_CRITERION_IDS
    assert reviewed[0].get("user_id")
