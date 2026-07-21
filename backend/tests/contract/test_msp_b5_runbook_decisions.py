"""PostgreSQL/API contract for MSP-B5 T4 analyst decisions."""
from __future__ import annotations

import os
from uuid import uuid4

from fastapi.testclient import TestClient

from app import db
from app.provenance import EvidencePointer
from app.runbook_match_decisions import PostgresRunbookMatchDecisionStore
from discovery.detectors.runbook_match import MATCH_PROPOSED, RunbookMatch


def _headers(token: str | None = None) -> dict[str, str]:
    if token is None:
        token = os.getenv("DEV_JWT", "dev-token-change-me")
    return {"Authorization": f"Bearer {token}"}


def _proposal(recurrence_id: str) -> RunbookMatch:
    return RunbookMatch(
        org_id="default",
        recurrence_id=recurrence_id,
        match_state=MATCH_PROPOSED,
        origin=MATCH_PROPOSED,
        runbook={"source_system": "document", "source_artifact": "runbook-42"},
        runbook_evidence=EvidencePointer.retrieved(
            source_system="document",
            source_artifact="runbook-42",
            source_timestamp="2026-07-20T00:00:00+00:00",
            chunk_id="chunk-42",
            retrieval_result_id="result-42",
            confidence=0.9,
        ).to_dict(),
        citing_incident_evidence=(),
        cited_references=(),
        match_confidence=0.9,
    )


def test_protected_api_persists_idempotent_history_and_labelled_feedback(
    client: TestClient,
) -> None:
    recurrence_id = f"rec-{uuid4().hex}"
    store = PostgresRunbookMatchDecisionStore()
    store.register_match(_proposal(recurrence_id))
    path = f"/api/runbook-matches/{recurrence_id}/decision"

    assert client.post(path, json={"action": "accept"}).status_code == 401
    viewer = os.getenv("VIEWER_JWT", "viewer-token")
    assert client.post(path, headers=_headers(viewer), json={"action": "accept"}).status_code == 403

    accepted = client.post(path, headers=_headers(), json={"action": "accept"})
    assert accepted.status_code == 200
    assert accepted.json()["current_state"] == "confirmed"
    assert accepted.json()["current_match"]["match_state"] == "confirmed"
    assert accepted.json()["changed"] is True

    repeated = client.post(path, headers=_headers(), json={"action": "accept"})
    assert repeated.status_code == 200
    assert repeated.json()["changed"] is False
    assert repeated.json()["revision"] == accepted.json()["revision"]

    dismissed = client.post(path, headers=_headers(), json={"action": "dismiss"})
    assert dismissed.status_code == 200
    assert dismissed.json()["current_state"] == "absent"
    assert dismissed.json()["current_match"] is None

    history = client.get(
        f"/api/runbook-matches/{recurrence_id}/decision-history",
        headers=_headers(),
    )
    assert history.status_code == 200
    assert [item["action"] for item in history.json()["decisions"]] == ["dismiss", "accept"]

    feedback = store.feedback("default")
    ours = [item for item in feedback if item["recurrence_id"] == recurrence_id]
    assert [item["feedback_label"] for item in ours] == [
        "runbook_match_accepted",
        "runbook_match_dismissed",
    ]

    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT count(*) FROM audit_log WHERE org_id = %s "
            "AND event_type = 'runbook_match_decided' "
            "AND payload LIKE %s",
            ("default", f'%"recurrence_id": "{recurrence_id}"%'),
        )
        assert cur.fetchone()[0] == 2
    finally:
        con.close()
