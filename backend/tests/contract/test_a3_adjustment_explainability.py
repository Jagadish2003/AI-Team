"""Contract tests for 2.0-A3 T3 — AC2, explainability, end to end.

AC2: *"Every rank-adjusted opportunity exposes a human-readable reason and links
to the contributing decisions/outcomes."*

What only a live stack can show, and is therefore tested here:

* the served finding carries the reason, and it is namespaced under ``_ranking``;
* the links RESOLVE — a contributing decision's ``href`` fetches the real
  decision, an outcome's fetches the real movement record;
* the reason never appears among confidence, corroboration or the evidence
  trace, on the actual payload rather than only in the code;
* the served copy passes the vocabulary guard at the boundary, not just at build
  time.

The structure and wording are unit-tested in ``tests/unit/test_learning_reason.py``
where they need no database.
"""

from __future__ import annotations

import os
from contextlib import closing
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app import db
from app.learning_reason import CREDIBILITY_FIELDS, reason_placement_violations
from app.learning_reason_vocabulary import scan_payload
from app.main import app

DEV_TOKEN = os.getenv("DEV_JWT", "dev-token-change-me")
VIEWER_TOKEN = os.getenv("VIEWER_JWT", "viewer-token")
BASE = "/api/learning/adjustment"


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture(autouse=True)
def _tables() -> None:
    from app.learning_adjustment_state import ensure_ranking_adjustment_tables
    from app.learning_feedback import ensure_opportunity_feedback_table

    ensure_opportunity_feedback_table()
    ensure_ranking_adjustment_tables()
    with closing(db.connect()) as con:
        with con.cursor() as cur:
            cur.execute(
                "SELECT to_regclass('public.ranking_adjustments'),"
                "       to_regclass('public.opportunity_feedback')"
            )
            adjustments, feedback = cur.fetchone()
    assert adjustments and feedback, (
        "learning tables are missing — check migrations 0036/0037 ran against "
        "the test database"
    )


def _auth(org_id: str, token: str = DEV_TOKEN) -> Dict[str, str]:
    return {"Authorization": f"Bearer {token}", "X-Org-Id": org_id}


def _seed_workspace_member(org_id: str, user_id: str, role: str = "owner") -> None:
    from app.rbac import _ensure_members_table

    _ensure_members_table()
    with closing(db.connect()) as con:
        with con.cursor() as cur:
            cur.execute(
                "INSERT INTO workspace_members (org_id, user_id, role, created_at) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                (org_id, user_id, role, datetime.now(timezone.utc).isoformat()),
            )
        con.commit()


def _org() -> str:
    org_id = f"org-a3t3-{uuid4().hex[:8]}"
    _seed_workspace_member(org_id, DEV_TOKEN)
    _seed_workspace_member(org_id, VIEWER_TOKEN, role="viewer")
    return org_id


def _identity() -> str:
    return f"opp_{uuid4().hex[:24]}"


def _feedback(org: str, action: str, detector: str):
    from app.learning_feedback import record_feedback

    return record_feedback(
        org, _identity(), action,
        actor_id="analyst_1", detector_id=detector, pack_id="service_cloud",
    )


def _opp(index: int, detector: str, impact: int = 7):
    return {
        "id": f"opp_{index:03d}",
        "opportunity_identity": f"ident_{index:03d}",
        "title": f"Finding {index}",
        "category": "Workflow",
        "tier": "Quick Win",
        "impact": impact,
        "effort": 3,
        "confidence": "HIGH",
        "aiRationale": "Case ownership changes cluster on one queue.",
        "evidenceIds": [f"ev_{index}"],
        "corroboration_sources": ["servicenow", "jira"],
        "corroboration_label": "Corroborated across ServiceNow and Jira",
        "triple_corroboration": False,
        "decision": "UNREVIEWED",
        "override": {"isLocked": False, "rationaleOverride": "", "overrideReason": "",
                     "updatedAt": None},
        "packId": "service_cloud",
        "_debug": {"detector_id": detector},
    }


def _run_with_opps(opps: List[Dict[str, Any]]) -> str:
    from app.db import run_kv_set
    from app.run_store import start_run_

    run_id = start_run_({"pack": "service_cloud"})["runId"]
    run_kv_set("opps", run_id, opps)
    return run_id


@pytest.fixture()
def adjusted(client):
    """An org with opposing signals and a run whose order actually moves."""
    org = _org()
    decisions = [_feedback(org, "accept", "FAVOURED") for _ in range(8)]
    for _ in range(8):
        _feedback(org, "dismiss", "DISFAVOURED")
    client.post(f"{BASE}/recompute", headers=_auth(org))

    opps = [_opp(i, "DISFAVOURED") for i in range(5)]
    opps += [_opp(i + 5, "FAVOURED") for i in range(5)]
    opps.append(_opp(10, "NEUTRAL"))
    run_id = _run_with_opps(opps)
    return {"org": org, "run_id": run_id, "opps": opps, "decisions": decisions}


def _moved_findings(client, ctx):
    served = client.get(
        f"/api/runs/{ctx['run_id']}/opportunities", headers=_auth(ctx["org"])
    ).json()
    return served, [o for o in served if o.get("_ranking", {}).get("moved")]


# ---------------------------------------------------------------------------
# AC2 — a human-readable reason on every adjusted opportunity
# ---------------------------------------------------------------------------


class TestEveryAdjustedFindingExposesAReason:
    def test_a_moved_finding_carries_a_rendered_sentence(self, client, adjusted):
        _, moved = _moved_findings(client, adjusted)
        assert moved, "nothing moved, so explainability is untested"
        for item in moved:
            summary = item["_ranking"]["reason"]["summary"]
            assert summary
            assert summary.startswith(("Ranked higher", "Ranked lower"))

    def test_the_reason_carries_the_structured_fields_not_only_prose(
        self, client, adjusted
    ):
        """A2 T4's pattern: countable and filterable, not a string to regex."""
        _, moved = _moved_findings(client, adjusted)
        reason = moved[0]["_ranking"]["reason"]
        for field in (
            "direction",
            "ranksMoved",
            "decisionCount",
            "decisionsByAction",
            "outcomeCount",
            "outcomesByVerdict",
            "wasCapped",
            "evidenceStrength",
        ):
            assert field in reason, f"missing structured field {field!r}"

    def test_an_unadjusted_finding_carries_no_reason(self, client, adjusted):
        """A reason with nothing to explain would bury the ones that have."""
        served, _ = _moved_findings(client, adjusted)
        for item in served:
            if item["_ranking"].get("adjusted") is False:
                assert "reason" not in item["_ranking"]

    def test_a_capped_unmoved_finding_still_carries_a_reason(self, client, adjusted):
        served, _ = _moved_findings(client, adjusted)
        capped = next(
            o for o in served
            if o["_ranking"].get("adjusted")
            and not o["_ranking"].get("moved")
            and o["_ranking"].get("wasCapped")
        )
        assert capped["_ranking"]["reason"]["summary"]
        assert capped["_ranking"]["reason"]["ranksMoved"] == 0

    def test_the_direction_matches_the_movement(self, client, adjusted):
        _, moved = _moved_findings(client, adjusted)
        for item in moved:
            ranking = item["_ranking"]
            expected = "up" if ranking["moved"] < 0 else "down"
            assert ranking["reason"]["direction"] == expected
            assert ranking["reason"]["ranksMoved"] == abs(ranking["moved"])

    def test_the_reason_counts_match_the_contributing_references(
        self, client, adjusted
    ):
        _, moved = _moved_findings(client, adjusted)
        reason = moved[0]["_ranking"]["reason"]
        assert reason["decisionCount"] == len(reason["contributingDecisions"])
        assert reason["outcomeCount"] == len(reason["contributingOutcomes"])


# ---------------------------------------------------------------------------
# AC2 — the links resolve
# ---------------------------------------------------------------------------


class TestTheLinksResolve:
    def test_a_contributing_decision_link_fetches_the_real_decision(
        self, client, adjusted
    ):
        """The half of AC2 that a count cannot satisfy."""
        _, moved = _moved_findings(client, adjusted)
        decisions = moved[0]["_ranking"]["reason"]["contributingDecisions"]
        assert decisions, "no contributing decisions linked"

        link = decisions[0]
        response = client.get(link["href"], headers=_auth(adjusted["org"]))
        assert response.status_code == 200, response.text
        assert response.json()["feedbackId"] == link["feedbackId"]
        assert response.json()["action"] == link["action"]

    def test_every_linked_decision_resolves(self, client, adjusted):
        _, moved = _moved_findings(client, adjusted)
        for link in moved[0]["_ranking"]["reason"]["contributingDecisions"]:
            assert (
                client.get(link["href"], headers=_auth(adjusted["org"])).status_code
                == 200
            )

    def test_a_cross_org_reader_cannot_resolve_the_links(self, client, adjusted):
        """AC6 holds through the explainability surface too."""
        other = _org()
        _, moved = _moved_findings(client, adjusted)
        link = moved[0]["_ranking"]["reason"]["contributingDecisions"][0]
        assert client.get(link["href"], headers=_auth(other)).status_code == 404

    def test_the_explain_route_returns_the_reason_for_one_finding(
        self, client, adjusted
    ):
        _, moved = _moved_findings(client, adjusted)
        opp_id = moved[0]["id"]
        body = client.get(
            f"{BASE}/explain/{adjusted['run_id']}/{opp_id}",
            headers=_auth(adjusted["org"]),
        ).json()
        assert body["opportunityId"] == opp_id
        assert body["reason"]["summary"]
        assert body["baseRank"] != body["adjustedRank"]

    def test_the_explain_route_404s_for_an_unadjusted_finding(self, client, adjusted):
        served, _ = _moved_findings(client, adjusted)
        unmoved = next(o for o in served if o["_ranking"].get("adjusted") is False)
        response = client.get(
            f"{BASE}/explain/{adjusted['run_id']}/{unmoved['id']}",
            headers=_auth(adjusted["org"]),
        )
        assert response.status_code == 404

    def test_the_explain_route_returns_capped_zero_displacement_reason(
        self, client, adjusted
    ):
        served, _ = _moved_findings(client, adjusted)
        capped = next(
            o for o in served
            if o["_ranking"].get("adjusted")
            and not o["_ranking"].get("moved")
            and o["_ranking"].get("wasCapped")
        )
        response = client.get(
            f"{BASE}/explain/{adjusted['run_id']}/{capped['id']}",
            headers=_auth(adjusted["org"]),
        )
        assert response.status_code == 200
        assert response.json()["reason"]["ranksMoved"] == 0

    def test_the_explain_route_requires_analyst(self, client, adjusted):
        response = client.get(
            f"{BASE}/explain/{adjusted['run_id']}/opp_000",
            headers=_auth(adjusted["org"], VIEWER_TOKEN),
        )
        assert response.status_code == 403


# ---------------------------------------------------------------------------
# The boundary — ordering only, never credibility
# ---------------------------------------------------------------------------


class TestTheReasonExplainsOrderingAndNothingElse:
    def test_the_served_finding_has_no_placement_violation(self, client, adjusted):
        """Checked on the real payload, not only in the code.

        AC3 forbids the adjustment touching evidence, confidence or
        corroboration. Copy sitting among them would tell a reader the learned
        signal contributed to the finding's credibility — the AC3-spirit
        violation this subtask must not commit.
        """
        served, _ = _moved_findings(client, adjusted)
        for item in served:
            assert reason_placement_violations(item) == [], item["id"]

    def test_the_reason_lives_only_under_the_ranking_namespace(
        self, client, adjusted
    ):
        _, moved = _moved_findings(client, adjusted)
        for item in moved:
            assert "reason" in item["_ranking"]
            assert "reason" not in item
            for field in CREDIBILITY_FIELDS:
                value = item.get(field)
                assert not isinstance(value, dict) or "reason" not in value

    def test_credibility_fields_are_unchanged_by_the_explanation(
        self, client, adjusted
    ):
        served, _ = _moved_findings(client, adjusted)
        original = {o["id"]: o for o in adjusted["opps"]}
        for item in served:
            source = original[item["id"]]
            for field in ("confidence", "corroboration_label", "corroboration_sources",
                          "evidenceIds", "aiRationale"):
                assert item[field] == source[field], (
                    f"{field} changed on {item['id']} — the explanation must "
                    "touch ordering only"
                )

    def test_the_reason_summary_never_mentions_confidence_or_corroboration(
        self, client, adjusted
    ):
        _, moved = _moved_findings(client, adjusted)
        for item in moved:
            summary = item["_ranking"]["reason"]["summary"].lower()
            for word in ("confiden", "corroborat", "more reliable", "verifie"):
                assert word not in summary, (
                    f"the reason implies a credibility contribution: {summary!r}"
                )


# ---------------------------------------------------------------------------
# The copy guard, at the boundary
# ---------------------------------------------------------------------------


class TestTheServedCopyNeverOverclaims:
    def test_the_served_opportunity_payload_is_clean(self, client, adjusted):
        """Enforced on what a customer actually receives, not only at build time."""
        served, _ = _moved_findings(client, adjusted)
        violations = [
            v
            for item in served
            for v in scan_payload(item.get("_ranking") or {}, path=item["id"])
        ]
        assert violations == [], [str(v) for v in violations]

    def test_the_explain_payload_is_clean(self, client, adjusted):
        _, moved = _moved_findings(client, adjusted)
        body = client.get(
            f"{BASE}/explain/{adjusted['run_id']}/{moved[0]['id']}",
            headers=_auth(adjusted["org"]),
        ).json()
        assert scan_payload(body) == []

    def test_the_preview_payload_is_clean(self, client, adjusted):
        body = client.get(
            f"{BASE}/preview/{adjusted['run_id']}", headers=_auth(adjusted["org"])
        ).json()
        assert scan_payload(body) == []

    def test_the_roadmap_payload_is_clean(self, client, adjusted):
        """Scoped to the LEARNING-OWNED regions, not the whole body.

        Sweeping the entire roadmap policed prose this guard does not own, and
        produced two false positives that CI caught: the shipped Next-30-Days
        summary "Prove value fast…" (fixed by narrowing the rule) and — the one
        that matters — a finding's own ``corroboration_label`` reading
        "Corroborated across ServiceNow and Jira".

        That second one must NOT be narrowed away. A finding legitimately
        carrying its corroboration status is exactly what the corroboration
        feature is for; a guard flagging it would be policing the evidence, which
        is how A1 T5 says a guard trains people to ignore it. The right fix is
        scope: sweep what learning wrote, and use
        :func:`reason_placement_violations` for the separate question of whether
        learning copy has LEAKED into a credibility field.
        """
        from app.db import run_kv_set
        from app.roadmap_engine import build_roadmap

        run_kv_set("roadmap", adjusted["run_id"], build_roadmap(adjusted["opps"]))
        body = client.get(
            f"/api/runs/{adjusted['run_id']}/roadmap", headers=_auth(adjusted["org"])
        ).json()

        findings = [
            item
            for stage in body.get("stages") or []
            for item in (stage.get("opportunities") or [])
        ]
        assert findings, "no roadmap opportunities to check"

        violations = [
            v
            for item in findings
            for v in scan_payload(item.get("_ranking") or {}, path=item.get("id", "?"))
        ]
        assert violations == [], [str(v) for v in violations]

    def test_the_roadmap_findings_keep_learning_copy_out_of_credibility_fields(
        self, client, adjusted
    ):
        """The other half of the roadmap boundary, asked the right way."""
        from app.db import run_kv_set
        from app.roadmap_engine import build_roadmap

        run_kv_set("roadmap", adjusted["run_id"], build_roadmap(adjusted["opps"]))
        body = client.get(
            f"/api/runs/{adjusted['run_id']}/roadmap", headers=_auth(adjusted["org"])
        ).json()
        for stage in body.get("stages") or []:
            for item in stage.get("opportunities") or []:
                assert reason_placement_violations(item) == [], item.get("id")

    def test_a_findings_own_corroboration_label_is_not_flagged(self, client, adjusted):
        """The guard must not police the corroboration feature's own prose.

        Pinned because narrowing the rule instead of the scope would have been
        the tempting fix, and would have blinded the guard to learning copy
        genuinely claiming corroboration.
        """
        from app.learning_reason_vocabulary import scan_text

        served, _ = _moved_findings(client, adjusted)
        labels = {o.get("corroboration_label") for o in served if o.get("corroboration_label")}
        assert labels, "no corroboration labels present, so this proves nothing"
        for label in labels:
            assert scan_text(label), (
                "expected the rule to match this text in isolation — the point is "
                "that the SWEEP is scoped so it never sees it"
            )


# ---------------------------------------------------------------------------
# Honesty about thin evidence, through the real stack
# ---------------------------------------------------------------------------


class TestHonestyThroughTheRealStack:
    def test_the_sentence_states_what_it_rests_on(self, client, adjusted):
        _, moved = _moved_findings(client, adjusted)
        for item in moved:
            assert "Based on" in item["_ranking"]["reason"]["summary"]

    def test_a_thin_adjustment_says_its_evidence_is_limited(self, client, adjusted):
        _, moved = _moved_findings(client, adjusted)
        thin = [
            o
            for o in moved
            if o["_ranking"]["reason"]["evidenceStrength"] in ("minimal", "limited")
        ]
        for item in thin:
            assert "limited evidence" in item["_ranking"]["reason"]["summary"]

    def test_the_evidence_strength_is_served_as_structured_data(
        self, client, adjusted
    ):
        """So a UI can style the hedge rather than string-matching the sentence."""
        _, moved = _moved_findings(client, adjusted)
        for item in moved:
            assert item["_ranking"]["reason"]["evidenceStrength"] in (
                "minimal",
                "limited",
                "moderate",
                "substantial",
            )
