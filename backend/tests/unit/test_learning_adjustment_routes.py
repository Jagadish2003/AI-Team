"""Focused regressions for the A3 learning-adjustment API edge."""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import routes_learning_adjustment as routes
from app.learning_adjustment import OpportunityAdjustment


def test_run_without_org_stamp_is_not_readable(monkeypatch):
    """A missing run org must not bypass tenancy isolation."""

    monkeypatch.setattr("app.run_store.read_run", lambda _run_id: {"runId": "run_1"})
    monkeypatch.setattr(routes, "get_current_org_id", lambda: "org_a")

    with pytest.raises(HTTPException) as excinfo:
        routes._read_run_for_org("run_1")

    assert excinfo.value.status_code == 404


def test_explain_route_returns_zero_displacement_adjustment(monkeypatch):
    """Numeric zero movement is still an adjustment record, not absence."""

    record = OpportunityAdjustment(
        opportunity_id="opp_zero",
        opportunity_identity="ident_zero",
        detector_id="detector_a",
        pack_id="pack_a",
        base_rank=2,
        adjusted_rank=2,
        base_impact=6.0,
        requested_delta=0.2,
        applied_delta=0.2,
        requested_rank_delta=0,
        net_weight=0.6,
        has_outcome_evidence=False,
        signal_count=1,
    )

    class FakeResult:
        policy = SimpleNamespace(max_score_fraction=0.15, max_rank_move=3)

        def by_opportunity_id(self):
            return {"opp_zero": record}

    monkeypatch.setattr(routes, "_read_run_for_org", lambda _run_id: {"runId": "run_1"})
    monkeypatch.setattr("app.db.run_kv_get", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(routes, "get_current_org_id", lambda: "org_a")
    monkeypatch.setattr(
        routes,
        "collect_learning_signals",
        lambda _org_id: SimpleNamespace(is_active=True, inactive_reason=None),
    )
    monkeypatch.setattr(routes, "get_adjustments", lambda _org_id: {})
    monkeypatch.setattr(routes, "adjust_ranking", lambda *_args, **_kwargs: FakeResult())

    body = routes.explain_adjustment("run_1", "opp_zero", _token="token")

    assert body["opportunityId"] == "opp_zero"
    assert body["baseRank"] == 2
    assert body["adjustedRank"] == 2
    assert body["reason"] is None
