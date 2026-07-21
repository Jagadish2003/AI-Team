"""API/schema contract checks for MSP-B5 T4."""
from __future__ import annotations

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.routing import APIRoute

from app import routes_runbook_matches as routes
from app.provenance import EvidencePointer
from app.runbook_match_decisions import InMemoryRunbookMatchDecisionStore
from app.security import require_auth
from database.models.runbook_match_decisions import ALL_RUNBOOK_MATCH_DDL
from discovery.detectors.runbook_match import MATCH_PROPOSED, RunbookMatch


def _proposal(org_id: str = "org-a") -> RunbookMatch:
    return RunbookMatch(
        org_id=org_id,
        recurrence_id="rec-001",
        match_state=MATCH_PROPOSED,
        origin=MATCH_PROPOSED,
        runbook={"source_system": "document", "source_artifact": "rb-1"},
        runbook_evidence=EvidencePointer.retrieved(
            source_system="document",
            source_artifact="rb-1",
            source_timestamp="2026-07-20T00:00:00+00:00",
            chunk_id="chunk-1",
            retrieval_result_id="result-1",
        ).to_dict(),
        citing_incident_evidence=(),
        cited_references=(),
        match_confidence=0.87,
    )


def test_schema_keys_current_history_and_feedback_by_org():
    ddl = "\n".join(ALL_RUNBOOK_MATCH_DDL).lower()
    assert "primary key (org_id, recurrence_id)" in ddl
    assert "runbook_match_decision_history" in ddl
    assert "unique (org_id, recurrence_id, revision)" in ddl
    assert "runbook_match_feedback" in ddl
    assert "feedback_label" in ddl


def test_routes_are_registered_once_and_require_auth_plus_analyst_role():
    app = FastAPI()
    routes.register_runbook_match_routes(app)
    routes.register_runbook_match_routes(app)
    api_routes = [route for route in app.routes if isinstance(route, APIRoute)]
    paths = [route.path for route in api_routes]
    assert paths.count("/api/runbook-matches/{recurrence_id}/decision") == 1
    decision_route = next(
        route for route in api_routes
        if route.path == "/api/runbook-matches/{recurrence_id}/decision"
    )
    dependency_calls = [dependency.call for dependency in decision_route.dependant.dependencies]
    assert require_auth in dependency_calls
    assert any(getattr(call, "__name__", "") == "_dependency" for call in dependency_calls)


def test_route_uses_authenticated_org_and_audits_only_real_changes(monkeypatch):
    store = InMemoryRunbookMatchDecisionStore()
    store.register_match(_proposal())
    audit_events = []
    monkeypatch.setattr(routes, "get_runbook_match_decision_store", lambda: store)
    monkeypatch.setattr(routes, "get_current_org_id", lambda: "org-a")
    monkeypatch.setattr(routes, "_get_user_id_from_token", lambda token: "analyst-7")
    monkeypatch.setattr(routes, "log_event", lambda event_type, **payload: audit_events.append((event_type, payload)))

    body = routes.RunbookMatchDecisionRequest(action="accept")
    first = routes.decide_runbook_match("rec-001", body, "signed-token")
    repeated = routes.decide_runbook_match("rec-001", body, "signed-token")
    assert first["current_state"] == "confirmed" and first["changed"] is True
    assert repeated["changed"] is False
    assert first["actor_id"] == "analyst-7"
    assert len(audit_events) == 1
    assert audit_events[0][1]["org_id"] == "org-a"

    monkeypatch.setattr(routes, "get_current_org_id", lambda: "org-b")
    with pytest.raises(HTTPException) as exc:
        routes.get_runbook_match_state("rec-001", "signed-token")
    assert exc.value.status_code == 404
