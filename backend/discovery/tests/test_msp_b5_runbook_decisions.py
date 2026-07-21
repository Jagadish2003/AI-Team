"""MSP-B5 T4 contract: persisted accept/dismiss/defer semantics."""
from __future__ import annotations

import pytest
from types import SimpleNamespace

from app.provenance import EvidencePointer
from app.runbook_match_decisions import (
    ACTION_ACCEPT,
    ACTION_DEFER,
    ACTION_DISMISS,
    FEEDBACK_ACCEPTED,
    FEEDBACK_DISMISSED,
    InMemoryRunbookMatchDecisionStore,
    RunbookMatchNotFound,
    build_current_runbook_composite,
)
from discovery.detectors.runbook_match import (
    MATCH_OBSERVED,
    MATCH_PROPOSED,
    RETRIEVAL_OK,
    RunbookMatch,
)


def _proposal(org_id: str = "org-a", recurrence_id: str = "rec-001") -> RunbookMatch:
    return RunbookMatch(
        org_id=org_id,
        recurrence_id=recurrence_id,
        match_state=MATCH_PROPOSED,
        origin=MATCH_PROPOSED,
        runbook={
            "source_system": "document",
            "source_artifact": "runbooks/restart.md",
            "title": None,
            "url": None,
            "identifiers": [],
        },
        runbook_evidence=EvidencePointer.retrieved(
            source_system="document",
            source_artifact="runbooks/restart.md",
            chunk_id="chunk-1",
            retrieval_result_id="result-1",
            source_timestamp="2026-07-20T00:00:00+00:00",
            confidence=0.89,
            source_artifact_type="record_id",
        ).to_dict(),
        citing_incident_evidence=(),
        cited_references=(),
        match_confidence=0.89,
    )


def test_defer_stays_proposed_and_repeated_submission_is_idempotent():
    store = InMemoryRunbookMatchDecisionStore()
    store.register_match(_proposal())
    first = store.decide("org-a", "rec-001", ACTION_DEFER, "analyst-1")
    repeated = store.decide("org-a", "rec-001", ACTION_DEFER, "analyst-1")
    assert first.changed is True and first.current_state == "proposed"
    assert repeated.changed is False and repeated.revision == first.revision
    assert len(store.history("org-a", "rec-001")) == 1
    assert store.feedback("org-a") == []


def test_accept_hardens_proposal_and_feeds_labelled_feedback():
    store = InMemoryRunbookMatchDecisionStore()
    store.register_match(_proposal())
    accepted = store.decide("org-a", "rec-001", ACTION_ACCEPT, "analyst-1")
    assert accepted.current_state == "confirmed"
    assert accepted.current_match is not None
    assert accepted.current_match.match_state == "confirmed"
    assert store.current("org-a", "rec-001")["current_match"]["match_state"] == "confirmed"
    feedback = store.feedback("org-a")
    assert [item["feedback_label"] for item in feedback] == [FEEDBACK_ACCEPTED]
    assert feedback[0]["features"]["runbook_source_artifact"] == "runbooks/restart.md"


def test_dismiss_removes_active_match_and_a_real_change_preserves_history():
    store = InMemoryRunbookMatchDecisionStore()
    store.register_match(_proposal())
    store.decide("org-a", "rec-001", ACTION_ACCEPT, "analyst-1")
    dismissed = store.decide("org-a", "rec-001", ACTION_DISMISS, "analyst-2")
    assert dismissed.current_state == "absent"
    assert dismissed.current_match is None
    assert store.current("org-a", "rec-001")["current_match"] is None
    history = store.history("org-a", "rec-001")
    assert [item["action"] for item in history] == [ACTION_DISMISS, ACTION_ACCEPT]
    assert history[0]["previous_state"] == "confirmed"
    assert [item["feedback_label"] for item in store.feedback("org-a")] == [
        FEEDBACK_ACCEPTED,
        FEEDBACK_DISMISSED,
    ]


def test_identical_recurrence_ids_are_isolated_by_organization():
    store = InMemoryRunbookMatchDecisionStore()
    store.register_match(_proposal("org-a", "same-id"))
    store.register_match(_proposal("org-b", "same-id"))
    store.decide("org-a", "same-id", ACTION_ACCEPT, "analyst-a")
    assert store.current("org-a", "same-id")["current_state"] == "confirmed"
    assert store.current("org-b", "same-id")["current_state"] == "proposed"
    assert store.feedback("org-b") == []
    with pytest.raises(RunbookMatchNotFound):
        store.current("org-c", "same-id")


def test_feedback_never_contains_document_text_or_incident_content():
    store = InMemoryRunbookMatchDecisionStore()
    store.register_match(_proposal())
    store.decide("org-a", "rec-001", ACTION_DISMISS, "analyst-1")
    serialized = repr(store.feedback("org-a")).lower()
    assert "content" not in serialized
    assert "note" not in serialized


def test_b6_integration_uses_current_decision_and_observed_source_truth_wins():
    store = InMemoryRunbookMatchDecisionStore()
    recurrence = SimpleNamespace(
        org_id="org-a", record_id="rec-001", example_evidence_pointers=()
    )
    proposal = _proposal()
    initial = build_current_runbook_composite(
        "org-a", recurrence, runbook_match=proposal,
        retrieval_status=RETRIEVAL_OK, store=store,
    )
    assert initial.runbook_state == "proposed"

    store.decide("org-a", "rec-001", ACTION_ACCEPT, "analyst-1")
    confirmed = build_current_runbook_composite(
        "org-a", recurrence, runbook_match=proposal,
        retrieval_status=RETRIEVAL_OK, store=store,
    )
    assert confirmed.runbook_state == "confirmed"

    store.decide("org-a", "rec-001", ACTION_DISMISS, "analyst-1")
    dismissed = build_current_runbook_composite(
        "org-a", recurrence, runbook_match=proposal,
        retrieval_status=RETRIEVAL_OK, store=store,
    )
    assert dismissed.runbook_state == "absent"
    assert dismissed.runbook_match is None

    observed = RunbookMatch.from_dict(
        {
            **proposal.as_dict(),
            "match_state": MATCH_OBSERVED,
            "origin": MATCH_OBSERVED,
            "citing_incident_evidence": [_proposal().runbook_evidence],
            "cited_references": ["RB-RESTART"],
            "match_confidence": None,
        }
    )
    direct = build_current_runbook_composite(
        "org-a", recurrence, runbook_match=observed,
        retrieval_status=RETRIEVAL_OK, store=store,
    )
    assert direct.runbook_state == "observed"
    assert direct.ranking_treatment == "strongest"
    # Earlier analyst actions remain audit history even though explicit source
    # truth now supplies the active match.
    assert len(store.history("org-a", "rec-001")) == 2
