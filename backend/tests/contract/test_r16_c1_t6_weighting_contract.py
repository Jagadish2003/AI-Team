"""
test_r16_c1_t6_weighting_contract.py

R16-C1 T6 — Contract tests per Section 5.

This suite proves the whole story end to end: a Stack Builder configuration
launched through POST /api/stack-builder/launch is persisted as the run's
setup_context, read back by the discovery weighting layer
(weighting_context.load_for_run), and visibly shapes scorer / corroboration
output. The decisive test is change-a-setting-and-results-move: launch two runs
on identical data that differ only in a system's role (or priority), then score
the same evidence under each and confirm the result moves in the expected
direction.

Unlike the discovery-layer unit tests (backend/discovery/tests/test_r16_c1_*),
these tests do NOT mock run_kv_get — they exercise the real API → database →
loader → scorer path so the wiring itself is under test.

Acceptance criteria covered (R16-C1 Section 5):
  AC1 - The scorer and corroboration engine read the per-system role and
        priority from the run's Stack Builder configuration.
  AC2 - Changing a system's role (System of Record → Supporting) and re-running
        on identical data produces visibly different scores in the expected
        direction.
  AC3 - Changing a system's priority produces a bounded, expected shift.
  AC4 - Weighting never breaches a hard rule: a Supporting-only, Slack-only, or
        single-source signal cannot reach HIGH regardless of priority.
  AC5 - Weighting modulates observed-evidence contribution and never promotes
        inferred evidence above observed.
  AC6 - Two runs with identical configuration and data produce identical
        results — the weighting is deterministic.
  AC7 - The setup screen's promise that roles/priorities affect weighting is
        proven true by the observable AC2/AC3 behaviour.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.db import get_run
from app.middleware.tenancy import DEV_DEFAULT_ORG

from discovery.weighting_context import load_for_run
from discovery.scorer import score
from discovery.models import DetectorResult
from discovery.t3_ceiling_clamp import apply_t3_ceiling_clamp
from discovery.provenance_guard import observed_beats_inferred


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def _auth() -> Dict[str, str]:
    return {"Authorization": f"Bearer {os.getenv('DEV_JWT', 'dev-token-change-me')}"}


#: A deliberately-wrong org placed in the launch request body. The architecture
#: invariant is that org_id is sourced ONLY from the verified JWT
#: (get_current_org_id() → TenancyMiddleware), never from the request body. The
#: launch route must ignore this value entirely; TestAC1TenancyEnforcement
#: proves the persisted run carries the JWT org rather than this sentinel.
_BODY_ORG_SENTINEL = "body-org-must-be-ignored"


# ─────────────────────────────────────────────────────────────────────────────
# Helpers — launch a run with a given Stack Builder weighting, score evidence
# ─────────────────────────────────────────────────────────────────────────────

def _launch(
    client: TestClient,
    *,
    sf_role: str,
    sf_priority: str,
    servicenow_role: str = "operational_signal_source",
    servicenow_priority: str = "secondary",
    jira_role: str = "operational_signal_source",
    jira_priority: str = "secondary",
    selected: Optional[list] = None,
) -> str:
    """Launch a run via the real endpoint and return its run_id.

    Only salesforce's role/priority varies between calls; everything else is
    held identical so a difference in results can only come from the weighting.
    """
    if selected is None:
        selected = ["salesforce", "servicenow"]
    body: Dict[str, Any] = {
        # Intentionally wrong: tenancy must source org_id from the JWT, not here.
        "org_id": _BODY_ORG_SENTINEL,
        "focus_id": "approvals_compliance",
        "industry_id": "financial_services",
        "template_id": None,
        "selected_system_ids": selected,
        "pack_id": "service_cloud",
        "weightings": {
            "salesforce": {
                "systemId": "salesforce",
                "role": sf_role,
                "priority": sf_priority,
                "workflowFocus": ["approvals"],
                "confirmed": True,
            },
            "servicenow": {
                "systemId": "servicenow",
                "role": servicenow_role,
                "priority": servicenow_priority,
                "workflowFocus": ["incident_management"],
                "confirmed": True,
            },
            "jira": {
                "systemId": "jira",
                "role": jira_role,
                "priority": jira_priority,
                "workflowFocus": ["backlog_work_queues"],
                "confirmed": True,
            },
        },
    }
    resp = client.post("/api/stack-builder/launch", headers=_auth(), json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()["runId"]


def _handoff_dr(signal_source: str = "salesforce", provenance: str = "observed") -> DetectorResult:
    """A moderate HANDOFF_FRICTION signal (proxy_ratio 1.75, high volume)."""
    return DetectorResult(
        detector_id="HANDOFF_FRICTION",
        signal_source=signal_source,
        metric_value=3.5,
        threshold=2.0,
        raw_evidence={"total_cases_90d": 800, "handoff_score": 4.0},
        provenance_type=provenance,
    )


def _borderline_high_dr(signal_source: str = "salesforce", provenance: str = "observed") -> DetectorResult:
    """A borderline-HIGH signal: proxy_ratio 2.105, volume 150.

    A system_of_record (weight 1.0) reaches HIGH on this; any discount below
    1.0 (supporting role, or inferred provenance) drops it under the HIGH bar.
    """
    return DetectorResult(
        detector_id="HANDOFF_FRICTION",
        signal_source=signal_source,
        metric_value=4.21,
        threshold=2.0,
        raw_evidence={"total_cases_90d": 150, "handoff_score": 4.0},
        provenance_type=provenance,
    )


def _run_data() -> Dict[str, Any]:
    return {
        "connected_systems": ["salesforce", "servicenow"],
        "servicenow": {
            "incidents": [
                {
                    "sys_created_on": datetime.now(timezone.utc).isoformat(),
                    "state": "Open",
                    "detector_ids": ["HANDOFF_FRICTION"],
                }
            ]
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# AC1 — scorer and corroboration read role/priority from the run configuration
# ─────────────────────────────────────────────────────────────────────────────

def _run_data_with_slack() -> Dict[str, Any]:
    data = _run_data()
    data["connected_systems"] = ["salesforce", "servicenow", "slack"]
    data["slack"] = {
        "escalation_pattern": {
            "fired": True,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    }
    return data


class TestAC1ReadsRunConfiguration:

    def test_loader_reads_persisted_role_and_priority(self, client):
        run_id = _launch(client, sf_role="system_of_record", sf_priority="primary")

        ctx = load_for_run(run_id)

        assert not ctx.is_neutral
        sf = ctx.get("salesforce")
        assert sf.role == "system_of_record"
        assert sf.priority == "primary"
        assert sf.confirmed is True

    def test_loader_reads_every_selected_system(self, client):
        run_id = _launch(client, sf_role="system_of_record", sf_priority="primary")

        ctx = load_for_run(run_id)

        assert "salesforce" in ctx.weightings
        assert "servicenow" in ctx.weightings
        assert ctx.get("servicenow").role == "operational_signal_source"

    def test_scorer_records_the_run_configuration(self, client):
        run_id = _launch(client, sf_role="system_of_record", sf_priority="primary")
        ctx = load_for_run(run_id)

        result = score(_handoff_dr("salesforce"), weighting_context=ctx)
        sw = result["score_debug"]["source_weighting"]

        assert sw is not None
        assert sw["system_id"] == "salesforce"
        assert sw["role"] == "system_of_record"
        assert sw["priority"] == "primary"

    def test_corroboration_engine_reads_the_run_configuration(self, client):
        run_id = _launch(client, sf_role="system_of_record", sf_priority="primary")
        ctx = load_for_run(run_id)

        try:
            from app.corroboration_engine import evaluate_corroboration
        except ModuleNotFoundError:
            from backend.app.corroboration_engine import evaluate_corroboration

        result = evaluate_corroboration(
            detector_id="HANDOFF_FRICTION",
            pack_id="service_cloud",
            run_data=_run_data(),
            run_timestamp=datetime.now(timezone.utc),
            org_id="test_org_r16c1_t6",
            weighting_context=ctx,
        )

        assert result is not None
        assert result.elevated_confidence in ("HIGH", "MEDIUM", "LOW")
        assert result.corroboration_weight_debug["applied"] is True
        assert result.corroboration_weight_debug["source_contributions"]["servicenow"]["role"] == (
            "operational_signal_source"
        )
        assert result.elevated_confidence == "MEDIUM"

    def test_corroboration_role_change_moves_elevation(self, client):
        """Changing the corroborating source role changes the corroboration verdict."""
        run_workflow = _launch(
            client,
            sf_role="system_of_record",
            sf_priority="primary",
            servicenow_role="workflow_system",
            servicenow_priority="secondary",
        )
        run_supporting = _launch(
            client,
            sf_role="system_of_record",
            sf_priority="primary",
            servicenow_role="operational_signal_source",
            servicenow_priority="secondary",
        )

        try:
            from app.corroboration_engine import evaluate_corroboration
        except ModuleNotFoundError:
            from backend.app.corroboration_engine import evaluate_corroboration

        workflow = evaluate_corroboration(
            detector_id="HANDOFF_FRICTION",
            pack_id="service_cloud",
            run_data=_run_data(),
            run_timestamp=datetime.now(timezone.utc),
            org_id="test_org_r16c1_t6",
            weighting_context=load_for_run(run_workflow),
        )
        supporting = evaluate_corroboration(
            detector_id="HANDOFF_FRICTION",
            pack_id="service_cloud",
            run_data=_run_data(),
            run_timestamp=datetime.now(timezone.utc),
            org_id="test_org_r16c1_t6",
            weighting_context=load_for_run(run_supporting),
        )

        assert workflow.elevated_confidence == "HIGH"
        assert supporting.elevated_confidence == "MEDIUM"
        assert workflow.corroboration_weight_debug["lead_sources"] == ["servicenow"]
        assert supporting.corroboration_weight_debug["lead_sources"] == []

    def test_corroboration_priority_change_moves_elevation(self, client):
        """Priority is bounded but real: workflow primary can lead; tertiary cannot."""
        run_primary = _launch(
            client,
            sf_role="system_of_record",
            sf_priority="primary",
            servicenow_role="workflow_system",
            servicenow_priority="primary",
        )
        run_tertiary = _launch(
            client,
            sf_role="system_of_record",
            sf_priority="primary",
            servicenow_role="workflow_system",
            servicenow_priority="tertiary",
        )

        try:
            from app.corroboration_engine import evaluate_corroboration
        except ModuleNotFoundError:
            from backend.app.corroboration_engine import evaluate_corroboration

        primary = evaluate_corroboration(
            detector_id="HANDOFF_FRICTION",
            pack_id="service_cloud",
            run_data=_run_data_with_slack(),
            run_timestamp=datetime.now(timezone.utc),
            org_id="test_org_r16c1_t6",
            weighting_context=load_for_run(run_primary),
        )
        tertiary = evaluate_corroboration(
            detector_id="HANDOFF_FRICTION",
            pack_id="service_cloud",
            run_data=_run_data_with_slack(),
            run_timestamp=datetime.now(timezone.utc),
            org_id="test_org_r16c1_t6",
            weighting_context=load_for_run(run_tertiary),
        )

        assert primary.elevated_confidence == "HIGH"
        assert "COR-06" in primary.rule_ids
        assert tertiary.elevated_confidence == "MEDIUM"
        assert "COR-05" in tertiary.rule_ids
        assert primary.corroboration_weight_debug["source_contributions"]["servicenow"]["source_weight"] == pytest.approx(0.88)
        assert tertiary.corroboration_weight_debug["source_contributions"]["servicenow"]["source_weight"] == pytest.approx(0.72)


# ─────────────────────────────────────────────────────────────────────────────
# AC1 (tenancy) — org_id comes from the JWT, never from the request body
# ─────────────────────────────────────────────────────────────────────────────

class TestAC1TenancyEnforcement:
    """The launch body carries a deliberately-wrong org_id (_BODY_ORG_SENTINEL).

    These tests prove the route ignores it and sources org from the verified
    JWT — so they fail (rather than silently passing on a 200) if a future
    handler ever trusts the request body for tenancy.
    """

    def test_persisted_run_uses_jwt_org_not_body_org(self, client):
        run_id = _launch(client, sf_role="system_of_record", sf_priority="primary")

        run = get_run(run_id)
        assert run is not None
        # The wrong body org must NOT have been persisted...
        assert run["orgId"] != _BODY_ORG_SENTINEL
        # ...and the JWT-derived dev org must have been used instead.
        assert run["orgId"] == DEV_DEFAULT_ORG

    def test_setup_context_org_matches_jwt_org_not_body(self, client):
        run_id = _launch(client, sf_role="system_of_record", sf_priority="primary")

        # The setup_context blob the weighting layer reads is also JWT-scoped.
        from app.db import run_kv_get
        ctx = run_kv_get("setup_context", run_id)
        assert ctx is not None
        assert ctx["org_id"] != _BODY_ORG_SENTINEL
        assert ctx["org_id"] == DEV_DEFAULT_ORG


# ─────────────────────────────────────────────────────────────────────────────
# AC2 — changing a role moves results in the expected direction (DECISIVE TEST)
# ─────────────────────────────────────────────────────────────────────────────

class TestAC2RoleChangeMovesResults:

    def test_demoting_record_to_supporting_reduces_impact(self, client):
        """The decisive change-a-setting test: identical data, role-only change."""
        run_record = _launch(client, sf_role="system_of_record", sf_priority="secondary")
        run_support = _launch(client, sf_role="operational_signal_source", sf_priority="secondary")

        dr = _handoff_dr("salesforce")
        impact_record = score(dr, weighting_context=load_for_run(run_record))["impact"]
        impact_support = score(dr, weighting_context=load_for_run(run_support))["impact"]

        assert impact_record > impact_support, (
            f"Demoting salesforce from system_of_record to supporting should "
            f"reduce authority: record={impact_record}, support={impact_support}"
        )

    def test_role_change_reduces_effective_proxy_ratio(self, client):
        run_record = _launch(client, sf_role="system_of_record", sf_priority="secondary")
        run_support = _launch(client, sf_role="operational_signal_source", sf_priority="secondary")

        dr = _handoff_dr("salesforce")
        epr_record = score(dr, weighting_context=load_for_run(run_record))["score_debug"]["effective_proxy_ratio"]
        epr_support = score(dr, weighting_context=load_for_run(run_support))["score_debug"]["effective_proxy_ratio"]

        assert epr_record > epr_support

    def test_workflow_system_lands_between_record_and_supporting(self, client):
        run_record = _launch(client, sf_role="system_of_record", sf_priority="secondary")
        run_workflow = _launch(client, sf_role="workflow_system", sf_priority="secondary")
        run_support = _launch(client, sf_role="operational_signal_source", sf_priority="secondary")

        dr = _handoff_dr("salesforce")
        rec = score(dr, weighting_context=load_for_run(run_record))["impact"]
        wfs = score(dr, weighting_context=load_for_run(run_workflow))["impact"]
        sup = score(dr, weighting_context=load_for_run(run_support))["impact"]

        assert rec >= wfs >= sup


# ─────────────────────────────────────────────────────────────────────────────
# AC3 — changing priority produces a bounded shift in emphasis
# ─────────────────────────────────────────────────────────────────────────────

class TestAC3PriorityChangeMovesResultsBounded:

    def test_primary_priority_emphasises_more_than_tertiary(self, client):
        run_primary = _launch(client, sf_role="operational_signal_source", sf_priority="primary")
        run_tertiary = _launch(client, sf_role="operational_signal_source", sf_priority="tertiary")

        dr = _handoff_dr("salesforce")
        epr_primary = score(dr, weighting_context=load_for_run(run_primary))["score_debug"]["effective_proxy_ratio"]
        epr_tertiary = score(dr, weighting_context=load_for_run(run_tertiary))["score_debug"]["effective_proxy_ratio"]

        assert epr_primary > epr_tertiary

    def test_priority_shift_is_bounded_to_the_nudge_range(self, client):
        """The shift is exactly the ±10% priority nudge — bounded, not unbounded.

        operational_signal_source weight = 0.6 × priority_nudge:
            primary  → 0.66    tertiary → 0.54
        so the ratio of effective proxy ratios is exactly 0.66 / 0.54.
        """
        run_primary = _launch(client, sf_role="operational_signal_source", sf_priority="primary")
        run_tertiary = _launch(client, sf_role="operational_signal_source", sf_priority="tertiary")

        dr = _handoff_dr("salesforce")
        epr_primary = score(dr, weighting_context=load_for_run(run_primary))["score_debug"]["effective_proxy_ratio"]
        epr_tertiary = score(dr, weighting_context=load_for_run(run_tertiary))["score_debug"]["effective_proxy_ratio"]

        assert epr_primary / epr_tertiary == pytest.approx(0.66 / 0.54, rel=1e-3)

    def test_priority_cannot_lift_supporting_above_record(self, client):
        """Even at primary priority, supporting weight (0.66) < record weight (1.0)."""
        run_support_primary = _launch(client, sf_role="operational_signal_source", sf_priority="primary")
        run_record_secondary = _launch(client, sf_role="system_of_record", sf_priority="secondary")

        dr = _handoff_dr("salesforce")
        epr_support = score(dr, weighting_context=load_for_run(run_support_primary))["score_debug"]["effective_proxy_ratio"]
        epr_record = score(dr, weighting_context=load_for_run(run_record_secondary))["score_debug"]["effective_proxy_ratio"]

        assert epr_support < epr_record


# ─────────────────────────────────────────────────────────────────────────────
# AC4 — hard rules hold: weighting cannot manufacture a false HIGH
# ─────────────────────────────────────────────────────────────────────────────

class TestAC4HardRulesHold:

    def test_record_baseline_can_reach_high(self, client):
        """Baseline: a system_of_record reaches HIGH on the borderline signal."""
        run_id = _launch(client, sf_role="system_of_record", sf_priority="secondary")
        result = score(_borderline_high_dr("salesforce"), weighting_context=load_for_run(run_id))
        assert result["confidence"] == "HIGH"

    def test_supporting_at_primary_priority_cannot_reach_high(self, client):
        """AC4: even at primary priority, a supporting source stays below HIGH."""
        run_id = _launch(client, sf_role="operational_signal_source", sf_priority="primary")
        result = score(_borderline_high_dr("salesforce"), weighting_context=load_for_run(run_id))
        assert result["confidence"] != "HIGH", (
            f"Supporting+primary must not reach HIGH, got {result['confidence']}"
        )

    def test_slack_only_corroboration_clamped_to_medium(self):
        assert apply_t3_ceiling_clamp("HIGH", corroboration_sources=["Slack"]) == "MEDIUM"

    def test_slack_plus_primary_corroborator_may_reach_high(self):
        # A non-Slack primary corroborator present → COR-06 path, HIGH allowed.
        assert apply_t3_ceiling_clamp(
            "HIGH", corroboration_sources=["Slack", "Salesforce"]
        ) == "HIGH"

    def test_single_source_clamped_to_medium(self):
        assert apply_t3_ceiling_clamp("HIGH", is_single_source=True) == "MEDIUM"

    def test_slack_system_id_clamped_even_if_misconfigured_as_record(self):
        # A customer wrongly assigning system_of_record to Slack cannot bypass it.
        assert apply_t3_ceiling_clamp(
            "HIGH", role="system_of_record", system_id="slack"
        ) == "MEDIUM"


# ─────────────────────────────────────────────────────────────────────────────
# AC5 — observed evidence is never outranked by inferred evidence
# ─────────────────────────────────────────────────────────────────────────────

class TestAC5ObservedBeatsInferred:

    def test_observed_effective_weight_at_least_inferred(self, client):
        """At the same high-priority source, observed weight >= inferred weight."""
        run_id = _launch(client, sf_role="system_of_record", sf_priority="primary")
        ctx = load_for_run(run_id)

        obs = score(_handoff_dr("salesforce", provenance="observed"), weighting_context=ctx)
        inf = score(_handoff_dr("salesforce", provenance="inferred"), weighting_context=ctx)

        obs_w = obs["score_debug"]["t4_provenance"]["effective_weight"]
        inf_w = inf["score_debug"]["t4_provenance"]["effective_weight"]

        assert observed_beats_inferred(obs_w, inf_w)
        assert obs_w >= inf_w
        assert obs["impact"] >= inf["impact"]

    def test_priority_nudge_stripped_from_inferred(self, client):
        """Inferred evidence is capped at the base role weight — no priority boost."""
        run_id = _launch(client, sf_role="system_of_record", sf_priority="primary")
        ctx = load_for_run(run_id)

        inf = score(_handoff_dr("salesforce", provenance="inferred"), weighting_context=ctx)
        t4 = inf["score_debug"]["t4_provenance"]

        # system_of_record base role weight is 1.0; primary nudge would push to 1.1.
        assert t4["effective_weight"] == pytest.approx(1.0)
        assert t4["weight_capped"] is True

    def test_inferred_cannot_reach_high_even_from_record(self, client):
        """A strong inferred signal from a system_of_record is ceilinged at MEDIUM."""
        run_id = _launch(client, sf_role="system_of_record", sf_priority="primary")
        ctx = load_for_run(run_id)

        result = score(_borderline_high_dr("salesforce", provenance="inferred"), weighting_context=ctx)
        assert result["confidence"] != "HIGH"


# ─────────────────────────────────────────────────────────────────────────────
# AC6 — determinism: identical configuration + data → identical results
# ─────────────────────────────────────────────────────────────────────────────

class TestAC6Determinism:

    def test_two_identical_runs_produce_identical_scores(self, client):
        run_a = _launch(client, sf_role="operational_signal_source", sf_priority="primary")
        run_b = _launch(client, sf_role="operational_signal_source", sf_priority="primary")

        dr = _handoff_dr("salesforce")
        a = score(dr, weighting_context=load_for_run(run_a))
        b = score(dr, weighting_context=load_for_run(run_b))

        assert a["impact"] == b["impact"]
        assert a["confidence"] == b["confidence"]
        assert a["score_debug"]["effective_proxy_ratio"] == b["score_debug"]["effective_proxy_ratio"]

    def test_repeated_scoring_of_one_run_is_stable(self, client):
        run_id = _launch(client, sf_role="system_of_record", sf_priority="primary")
        ctx = load_for_run(run_id)
        dr = _handoff_dr("salesforce")

        impacts = {score(dr, weighting_context=ctx)["impact"] for _ in range(5)}
        assert len(impacts) == 1


# ─────────────────────────────────────────────────────────────────────────────
# AC7 — the setup-screen promise is proven true by observable behaviour
# ─────────────────────────────────────────────────────────────────────────────

class TestAC7PromiseProvenTrue:

    def test_role_choice_observably_changes_the_finding(self, client):
        """The Screen 3 promise ("weight evidence correctly") is real: the only
        difference between these two runs is salesforce's role, and the finding
        moves — the authoritative role reaches HIGH, the supporting role does not.
        """
        run_record = _launch(client, sf_role="system_of_record", sf_priority="primary")
        run_support = _launch(client, sf_role="operational_signal_source", sf_priority="primary")

        dr = _borderline_high_dr("salesforce")
        record = score(dr, weighting_context=load_for_run(run_record))
        support = score(dr, weighting_context=load_for_run(run_support))

        assert record["confidence"] == "HIGH"
        assert support["confidence"] != "HIGH"
        assert record["impact"] >= support["impact"]

    def test_configuration_that_runs_is_the_one_selected(self, client):
        """The configuration the customer selected is the configuration that runs:
        the persisted run config round-trips to the exact role/priority chosen.
        """
        run_id = _launch(client, sf_role="workflow_system", sf_priority="primary")
        sf = load_for_run(run_id).get("salesforce")
        assert (sf.role, sf.priority) == ("workflow_system", "primary")
