"""
R18-C0 P8 — Re-editable review decisions (audit-preserving).

AC8 (Testable): "Changing an Approve/Reject decision creates a new event
preserving the prior decision, actor, and timestamp; the full decision history
is queryable."

The backend must APPEND a new audit/feedback event on every decision change —
never overwrite the prior one — so outcome tracking (2.0) and audit both keep
the full timeline. These tests pin that append-not-overwrite behavior.
"""
import os


def _auth_headers():
    token = os.getenv("DEV_JWT", "dev-token-change-me")
    return {"Authorization": f"Bearer {token}"}


def _start_run(client):
    r = client.post(
        "/api/runs/start",
        headers=_auth_headers(),
        json={
            "connectedSources": ["ServiceNow", "Jira"],
            "uploadedFiles": ["incident_data.csv"],
            "sampleWorkspaceEnabled": False,
            "mode": "offline",
        },
    )
    assert r.status_code == 200
    return r.json()["runId"]


def _opp_id(client, run_id):
    opps = client.get(
        f"/api/runs/{run_id}/opportunities", headers=_auth_headers()
    ).json()
    assert isinstance(opps, list) and len(opps) > 0
    return opps[0]["id"]


def _decision_events(client, run_id, opp_id):
    """Audit events for this opportunity that are Approve/Reject decisions."""
    audit = client.get(f"/api/runs/{run_id}/audit", headers=_auth_headers()).json()
    assert isinstance(audit, list)
    return [
        e
        for e in audit
        if e.get("opportunityId") == opp_id
        and e.get("action") in ("APPROVED", "REJECTED", "UNREVIEWED")
    ]


def test_changing_a_decision_appends_and_preserves_prior(client):
    """Reject -> Approve creates a SECOND event; the first Reject is preserved."""
    run_id = _start_run(client)
    opp_id = _opp_id(client, run_id)

    # First decision: REJECTED
    r1 = client.post(
        f"/api/runs/{run_id}/opportunities/{opp_id}/decision",
        headers=_auth_headers(),
        json={"decision": "REJECTED"},
    )
    assert r1.status_code == 200
    assert r1.json()["decision"] == "REJECTED"

    after_first = _decision_events(client, run_id, opp_id)
    assert len(after_first) == 1
    assert after_first[0]["action"] == "REJECTED"

    # Change the decision: APPROVED
    r2 = client.post(
        f"/api/runs/{run_id}/opportunities/{opp_id}/decision",
        headers=_auth_headers(),
        json={"decision": "APPROVED"},
    )
    assert r2.status_code == 200
    # Current decision on the opportunity is the new one.
    assert r2.json()["decision"] == "APPROVED"

    after_change = _decision_events(client, run_id, opp_id)
    # AC8: a NEW event was appended, not an overwrite — both events remain.
    assert len(after_change) == 2, (
        "Changing a decision must append a new audit event, preserving the "
        f"prior one. Got events: {after_change}"
    )

    # The prior REJECTED event is still present (append, never overwrite).
    actions = [e["action"] for e in after_change]
    assert "REJECTED" in actions and "APPROVED" in actions

    # Newest-first: the change event (APPROVED) leads, and it records the
    # actor, a timestamp, and the prior decision it replaced.
    newest = after_change[0]
    assert newest["action"] == "APPROVED"
    assert newest.get("by"), "audit event must record the actor"
    assert int(newest.get("tsEpoch", 0)) > 0, "audit event must record a timestamp"
    assert newest.get("previousDecision") == "REJECTED", (
        "the change event must preserve the prior decision it replaced"
    )


def test_full_decision_history_is_queryable_and_ordered(client):
    """Multiple flips build a queryable, newest-first decision timeline."""
    run_id = _start_run(client)
    opp_id = _opp_id(client, run_id)

    sequence = ["APPROVED", "REJECTED", "APPROVED"]
    for decision in sequence:
        resp = client.post(
            f"/api/runs/{run_id}/opportunities/{opp_id}/decision",
            headers=_auth_headers(),
            json={"decision": decision},
        )
        assert resp.status_code == 200

    events = _decision_events(client, run_id, opp_id)
    # Every change is recorded — the full history is queryable.
    assert len(events) == len(sequence), (
        f"Expected {len(sequence)} decision events, got {len(events)}: {events}"
    )

    # Audit endpoint returns newest-first by tsEpoch.
    epochs = [int(e.get("tsEpoch", 0)) for e in events]
    assert epochs == sorted(epochs, reverse=True), (
        f"Decision history must be newest-first. Got: {epochs}"
    )

    # The current decision on the opportunity matches the last write.
    opp = next(
        o
        for o in client.get(
            f"/api/runs/{run_id}/opportunities", headers=_auth_headers()
        ).json()
        if o["id"] == opp_id
    )
    assert opp["decision"] == sequence[-1]


def test_repeated_same_decision_does_not_append_a_duplicate(client):
    """A no-op re-click of the same decision is not a change — no new event."""
    run_id = _start_run(client)
    opp_id = _opp_id(client, run_id)

    for _ in range(3):
        resp = client.post(
            f"/api/runs/{run_id}/opportunities/{opp_id}/decision",
            headers=_auth_headers(),
            json={"decision": "APPROVED"},
        )
        assert resp.status_code == 200

    events = _decision_events(client, run_id, opp_id)
    assert len(events) == 1, (
        "Re-submitting the same decision must not append duplicate audit "
        f"events. Got: {events}"
    )
