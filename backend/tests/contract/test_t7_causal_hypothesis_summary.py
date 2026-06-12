"""Contract tests for T7 (ENT-6 / T3-S16-A).

CausalHypothesisSummary surfaced on OppEnrichment, populated live from the
causal_hypotheses row for the current run and opportunity (not run-scoped KV).

Coverage:
  - CausalHypothesisSummary carries exactly the six required fields.
  - OppEnrichment.causal_hypothesis defaults to None and serialises (all six
    fields present, including a null preliminary_reason).
  - Loader (_load_causal_hypothesis): populated + None cases, exact-run scoped,
    org-scoped, degrades to None without an org.
  - Endpoint response: causal_hypothesis populated when a row exists, null when
    absent. Scoring fields are never touched.
"""
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Dict
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app import db
from app.routes_sprint4_t6 import (
    CausalHypothesisSummary,
    OppEnrichment,
    _load_causal_hypothesis,
)
from database.models.causal_hypotheses import CausalHypothesis


REQUIRED_FIELDS = {
    "cause_chain", "falsifiability_condition", "confidence",
    "inferred", "preliminary", "preliminary_reason",
}

_FALSIFIABILITY = (
    "If covenant review completion rate does not improve within 90 days when "
    "loan origination volume returns to the 90-day baseline, the hypothesis is wrong."
)


def _auth(org_id: str = "default") -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {os.getenv('DEV_JWT', 'dev-token-change-me')}",
        "X-Org-Id": org_id,
    }


def _seed_workspace_member(org_id: str) -> None:
    with sqlite3.connect(os.environ["DB_PATH"]) as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO workspace_members (org_id, user_id, role, created_at)
            VALUES (?, ?, 'owner', ?)
            """,
            (org_id, os.getenv("DEV_JWT", "dev-token-change-me"), "2026-01-01T00:00:00+00:00"),
        )
        conn.commit()


def _insert_causal(
    org_id: str,
    opp_id: str,
    run_id: str = "run-causal",
    *,
    created_at: datetime,
    preliminary: bool = False,
    preliminary_reason=None,
    cause_chain=None,
    confidence: float = 0.8,
    inferred: bool = False,
) -> str:
    """Insert one causal_hypotheses row via the T1 model + direct INSERT."""
    hypothesis = CausalHypothesis(
        org_id=org_id,
        opportunity_id=opp_id,
        run_id=run_id,
        cause_chain=cause_chain or [
            "Loan origination volume rose 40% [OBSERVED].",
            "Capacity was not scaled [OBSERVED].",
            "Covenant review queue backed up [OBSERVED].",
        ],
        evidence_links=["e1", "e2"],
        confidence=confidence,
        inferred=inferred,
        falsifiability_condition=_FALSIFIABILITY,
        preliminary=preliminary,
        gate_run_count=12,
        generated_by="llm",
        temporal_support={"svc::e1::metric_value": {"run_count": 12}},
        preliminary_reason=preliminary_reason,
        created_at=created_at,
    )
    row = hypothesis.to_db_row()
    columns = (
        "id", "org_id", "opportunity_id", "run_id", "cause_chain", "evidence_links",
        "temporal_support", "confidence", "inferred", "falsifiability_condition",
        "preliminary", "preliminary_reason", "gate_run_count", "generated_by", "created_at",
    )
    placeholders = ", ".join("?" for _ in columns)
    with sqlite3.connect(os.environ["DB_PATH"]) as conn:
        conn.execute(
            f"INSERT INTO causal_hypotheses ({', '.join(columns)}) VALUES ({placeholders})",
            tuple(row[c] for c in columns),
        )
        conn.commit()
    return row["id"]


def _now() -> datetime:
    return datetime(2026, 6, 10, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Model shape
# ---------------------------------------------------------------------------

class TestCausalHypothesisSummaryShape:
    def test_has_all_six_fields(self):
        summary = CausalHypothesisSummary(
            cause_chain=["a", "b"],
            falsifiability_condition="If x then y.",
            confidence=0.8,
            inferred=False,
            preliminary=False,
            preliminary_reason=None,
        )
        assert set(summary.model_dump().keys()) == REQUIRED_FIELDS

    def test_oppenrichment_causal_hypothesis_defaults_none(self):
        enr = OppEnrichment(oppId="opp-1")
        assert enr.causal_hypothesis is None

    def test_oppenrichment_serialises_causal_hypothesis_with_null_reason(self):
        """All six fields present in the serialised response, even null reason."""
        summary = CausalHypothesisSummary(
            cause_chain=["step 1", "step 2"],
            falsifiability_condition="If x does not change, the hypothesis is wrong.",
            confidence=0.75,
            inferred=True,
            preliminary=False,
            preliminary_reason=None,
        )
        dumped = OppEnrichment(oppId="opp-1", causal_hypothesis=summary).model_dump()
        assert set(dumped["causal_hypothesis"].keys()) == REQUIRED_FIELDS
        assert dumped["causal_hypothesis"]["preliminary_reason"] is None
        assert dumped["causal_hypothesis"]["inferred"] is True

    def test_oppenrichment_causal_hypothesis_omitted_serialises_none(self):
        dumped = OppEnrichment(oppId="opp-1").model_dump()
        assert "causal_hypothesis" in dumped
        assert dumped["causal_hypothesis"] is None


# ---------------------------------------------------------------------------
# Loader — _load_causal_hypothesis
# ---------------------------------------------------------------------------

class TestLoadCausalHypothesis:
    def test_populated_maps_all_six_fields(self):
        org = f"org-t7c-pop-{uuid4().hex[:8]}"
        opp = "opp-pop"
        _insert_causal(
            org, opp, created_at=_now(),
            preliminary=True,
            preliminary_reason="gate2_unresolved_entities: 1 entities require resolution",
            cause_chain=["Step one [OBSERVED].", "Step two [OBSERVED]."],
            confidence=0.72,
            inferred=False,
        )
        summary = _load_causal_hypothesis(org, opp, "run-causal")
        assert summary is not None
        assert summary.cause_chain == ["Step one [OBSERVED].", "Step two [OBSERVED]."]
        assert summary.falsifiability_condition == _FALSIFIABILITY
        assert summary.confidence == 0.72
        assert summary.inferred is False
        assert summary.preliminary is True
        assert summary.preliminary_reason == "gate2_unresolved_entities: 1 entities require resolution"

    def test_absent_returns_none(self):
        org = f"org-t7c-none-{uuid4().hex[:8]}"
        assert _load_causal_hypothesis(org, "opp-does-not-exist", "run-missing") is None

    def test_surfaces_only_the_requested_run(self):
        org = f"org-t7c-recent-{uuid4().hex[:8]}"
        opp = "opp-recent"
        older = _now() - timedelta(days=2)
        newer = _now()
        _insert_causal(org, opp, run_id="run-old", created_at=older,
                       preliminary=True, preliminary_reason="gate1_insufficient_run_count: 7 of 10 runs completed")
        _insert_causal(org, opp, run_id="run-new", created_at=newer,
                       preliminary=False, preliminary_reason=None, confidence=0.9)
        old_summary = _load_causal_hypothesis(org, opp, "run-old")
        new_summary = _load_causal_hypothesis(org, opp, "run-new")
        assert old_summary is not None
        assert old_summary.preliminary is True
        assert old_summary.preliminary_reason is not None
        assert new_summary is not None
        assert new_summary.preliminary is False
        assert new_summary.preliminary_reason is None
        assert new_summary.confidence == 0.9

    def test_cross_org_isolation(self):
        org_a = f"org-t7c-a-{uuid4().hex[:8]}"
        org_b = f"org-t7c-b-{uuid4().hex[:8]}"
        opp = "opp-shared-id"
        _insert_causal(org_a, opp, created_at=_now())
        assert _load_causal_hypothesis(org_b, opp, "run-causal") is None

    def test_missing_org_returns_none(self):
        assert _load_causal_hypothesis(None, "opp-x", "run-x") is None
        assert _load_causal_hypothesis("", "opp-x", "run-x") is None


# ---------------------------------------------------------------------------
# Endpoint response — populated vs null
# ---------------------------------------------------------------------------

class TestEnrichmentEndpointCausalHypothesis:
    @pytest.fixture(scope="class")
    def client(self):
        from app.main import app
        return TestClient(app)

    def _seed_run(self, org_id: str, run_id: str, opp_id: str) -> None:
        _seed_workspace_member(org_id)
        db.run_set(run_id, {"id": run_id, "runId": run_id, "status": "complete", "orgId": org_id})
        db.run_kv_set("opps", run_id, [{"id": opp_id, "aiRationale": "rationale"}])

    def test_causal_hypothesis_populated_in_response(self, client):
        org = f"org-t7c-ep-pop-{uuid4().hex[:6]}"
        run = f"run-t7c-ep-pop-{uuid4().hex[:6]}"
        opp = "opp-1"
        self._seed_run(org, run, opp)
        _insert_causal(
            org, opp, run_id=run, created_at=_now(),
            preliminary=False, confidence=0.85,
        )

        r = client.get(f"/api/runs/{run}/opportunities/{opp}/enrichment", headers=_auth(org))
        assert r.status_code == 200, r.text
        ch = r.json()["causal_hypothesis"]
        assert ch is not None
        assert set(ch.keys()) == REQUIRED_FIELDS
        assert ch["preliminary"] is False
        assert ch["confidence"] == 0.85
        assert ch["falsifiability_condition"] == _FALSIFIABILITY

    def test_causal_hypothesis_null_when_absent(self, client):
        org = f"org-t7c-ep-none-{uuid4().hex[:6]}"
        run = f"run-t7c-ep-none-{uuid4().hex[:6]}"
        opp = "opp-1"
        self._seed_run(org, run, opp)  # no causal row

        r = client.get(f"/api/runs/{run}/opportunities/{opp}/enrichment", headers=_auth(org))
        assert r.status_code == 200, r.text
        body = r.json()
        assert "causal_hypothesis" in body
        assert body["causal_hypothesis"] is None

    def test_preliminary_hypothesis_surfaces_reason(self, client):
        org = f"org-t7c-ep-prelim-{uuid4().hex[:6]}"
        run = f"run-t7c-ep-prelim-{uuid4().hex[:6]}"
        opp = "opp-1"
        self._seed_run(org, run, opp)
        _insert_causal(
            org, opp, run_id=run, created_at=_now(),
            preliminary=True,
            preliminary_reason="gate3_inferred_primary_step: step 3",
            inferred=True,
        )

        r = client.get(f"/api/runs/{run}/opportunities/{opp}/enrichment", headers=_auth(org))
        assert r.status_code == 200, r.text
        ch = r.json()["causal_hypothesis"]
        assert ch["preliminary"] is True
        assert ch["preliminary_reason"] == "gate3_inferred_primary_step: step 3"
        assert ch["inferred"] is True
