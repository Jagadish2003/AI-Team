"""MSP-B5 T6 contract suite covering AC1-AC7 end to end.

The suite composes the real recurrence, citation-resolution, runbook retrieval,
scoring, lifecycle, B6 presentation, documentation-gap, redaction, and org-scope
contracts. All external reads are deterministic fakes; no network, model, or
database is required.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

import pytest

os.environ["INGEST_MODE"] = "offline"

from app.opportunity_display import (  # noqa: E402
    with_display,
    with_exec_report_display_titles,
    with_roadmap_display_titles,
)
from app.provenance import EvidencePointer  # noqa: E402
from app.runbook_match_decisions import (  # noqa: E402
    ACTION_ACCEPT,
    ACTION_DEFER,
    ACTION_DISMISS,
    FEEDBACK_ACCEPTED,
    FEEDBACK_DISMISSED,
    InMemoryRunbookMatchDecisionStore,
    build_current_runbook_composite,
)
from discovery.detectors.ops_recurrence import (  # noqa: E402
    RecurrenceConfig,
    find_recurrences,
)
from discovery.detectors.runbook_composite import (  # noqa: E402
    LABEL_ABSENT,
    LABEL_CONFIRMED,
    LABEL_OBSERVED,
    LABEL_PROPOSED,
    LABEL_UNAVAILABLE,
    RUNBOOK_UNAVAILABLE,
    build_documented_repeated_manual_composite,
)
from discovery.detectors.runbook_documentation_gap import (  # noqa: E402
    EVALUATION_GAP,
    EVALUATION_MATCHED,
    EVALUATION_UNAVAILABLE,
    DocumentationGapConfig,
    evaluate_documentation_gap,
)
from discovery.detectors.runbook_pipeline import (  # noqa: E402
    evaluate_runbook_recurrence,
    evaluate_runbook_recurrences,
)
from discovery.detectors.runbook_match import (  # noqa: E402
    CITATION_RESOLUTION_OK,
    MATCH_CONFIRMED,
    MATCH_OBSERVED,
    MATCH_PROPOSED,
    RETRIEVAL_OK,
    RETRIEVAL_UNAVAILABLE,
    InMemoryRunbookLibrary,
    RunbookCandidate,
    RunbookPage,
    RunbookRetrievalResult,
    RunbookScoringConfig,
    propose_runbook_match,
    resolve_runbook_citations,
    retrieve_runbook_candidates,
)
from discovery.ingest.secret_redaction import scan_and_redact  # noqa: E402
from discovery.signals.evidence_store import OrgScopeError  # noqa: E402
from discovery.signals.resolution_signature import (  # noqa: E402
    compute_incident_identity_signature,
    compute_resolution_signature,
)

_AS_OF = "2026-07-20 12:00:00"
_RUNBOOK_ID = "KB0012345"


def _incident(
    number: int,
    *,
    org_id: str = "org-a",
    runbook_refs: tuple[str, ...] = (),
) -> dict:
    resolved_at = f"2026-07-{10 + number:02d} 12:00:00"
    sys_id = f"{org_id}-incident-{number:03d}"
    category = "software"
    ci_class = "cmdb_ci_server"
    close_code = "Solved (Permanently)"
    group = "Platform Operations"
    short_description = "Message service unavailable"
    evidence = EvidencePointer.observed(
        source_system="servicenow",
        source_artifact=sys_id,
        source_timestamp=resolved_at,
        source_artifact_type="record_id",
    ).to_dict()
    return {
        "sys_id": sys_id,
        "number": f"INC{number:07d}",
        "org_id": org_id,
        "category": category,
        "ci_class": ci_class,
        "short_description": short_description,
        "assignment_group": group,
        "resolved_at": resolved_at,
        "resolution": {
            "is_resolved": True,
            "resolution_category": category,
            "close_code": close_code,
            "resolved_by_group": group,
            "resolved_at": resolved_at,
            "time_to_resolve_seconds": number * 600,
            "incident_identity_signature": compute_incident_identity_signature(
                category=category,
                short_description=short_description,
                ci_class=ci_class,
            ),
            "resolution_signature": compute_resolution_signature(
                category=category,
                close_code=close_code,
                resolved_by_group=group,
                ci_class=ci_class,
            ),
            "incident_sys_id": sys_id,
            "evidence": evidence,
            "notes_evidence": dict(evidence),
            "runbook_references": list(runbook_refs),
        },
    }


def _recurrence(
    *,
    org_id: str = "org-a",
    count: int = 5,
    runbook_refs: tuple[str, ...] = (),
):
    incidents = [
        _incident(i, org_id=org_id, runbook_refs=runbook_refs)
        for i in range(1, count + 1)
    ]
    records = find_recurrences(
        {
            "org_id": org_id,
            "incident_metrics": {"org_id": org_id, "incidents": incidents},
        },
        config=RecurrenceConfig(floor=3, window_days=30, max_examples=3),
        as_of=_AS_OF,
        org_id=org_id,
    )
    assert len(records) == 1
    return records[0]


def _page(
    *, org_id: str = "org-a", artifact: str = "runbooks/message-service"
) -> RunbookPage:
    return RunbookPage(
        org_id=org_id,
        source_system="confluence",
        source_artifact=artifact,
        identifiers=(_RUNBOOK_ID,),
        title="Restore message service",
        url=f"https://docs.example/{org_id}/message-service",
        source_timestamp="2026-07-01T00:00:00+00:00",
    )


@dataclass(frozen=True)
class _Chunk:
    content: str = (
        "software solved permanently cmdb_ci_server platform operations "
        "restart the message service"
    )
    similarity: float = 0.84
    source_system: str = "confluence"
    source_artifact: str = "runbooks/message-service"
    chunk_id: str = "chunk-message-service"
    retrieval_result_id: str = "retrieval-message-service"
    source_timestamp: str = "2026-07-01T00:00:00+00:00"
    is_stale: bool = False


class _Retrieve:
    def __init__(self, result=(), error: Exception | None = None) -> None:
        self.result = list(result)
        self.error = error
        self.calls: list[dict] = []

    def __call__(
        self,
        org_id,
        query_text,
        *,
        k,
        source_filter,
        min_score,
        include_stale,
    ):
        self.calls.append(
            {
                "org_id": org_id,
                "query_text": query_text,
                "k": k,
                "source_filter": tuple(source_filter),
                "min_score": min_score,
                "include_stale": include_stale,
            }
        )
        if self.error is not None:
            raise self.error
        return list(self.result)


def _semantic_result(rec, *, org_id: str = "org-a", similarity: float = 0.84):
    retrieve = _Retrieve([_Chunk(similarity=similarity)])
    result = retrieve_runbook_candidates(
        org_id,
        rec,
        retrieve_fn=retrieve,
        embedding_available_fn=lambda query: True,
    )
    return result, retrieve


def test_ac1_explicit_citation_is_deterministic_observed_and_evidenced_on_both_sides():
    rec = _recurrence(count=5, runbook_refs=(_RUNBOOK_ID,))
    library = InMemoryRunbookLibrary([_page()])

    first = resolve_runbook_citations("org-a", rec, library)
    second = resolve_runbook_citations("org-a", rec, library)

    assert first.as_dict() == second.as_dict()
    assert first.status == CITATION_RESOLUTION_OK
    assert first.match is not None
    assert first.match.match_state == first.match.origin == MATCH_OBSERVED
    assert first.match.match_confidence is None
    assert {p["source_artifact"] for p in first.match.citing_incident_evidence} == {
        f"org-a-incident-{i:03d}" for i in range(1, 6)
    }
    assert first.match.runbook_evidence["source_artifact"] == (
        "runbooks/message-service"
    )
    assert first.match.runbook_evidence["origin"] == "observed"


def test_ac2_semantic_match_stays_proposed_in_finding_report_and_ui_data():
    rec = _recurrence(count=5)
    retrieval, called = _semantic_result(rec)
    match = propose_runbook_match("org-a", rec, retrieval.candidates)

    assert called.calls[0]["org_id"] == "org-a"
    assert match is not None
    assert match.match_state == match.origin == MATCH_PROPOSED
    assert isinstance(match.match_confidence, float)
    assert match.match_confidence >= 0.75
    assert match.citing_incident_evidence == ()

    composite = build_documented_repeated_manual_composite(
        "org-a", rec, runbook_match=match, retrieval_status=retrieval.status
    ).as_dict()
    opportunity = {
        "id": "opp-runbook-proposal",
        "title": "Repeated message-service recovery",
        "impact": 4,
        "effort": 2,
        "runbook_composite": composite,
    }
    finding = with_display(opportunity)
    report = with_exec_report_display_titles({"topQuickWins": [opportunity]})[
        "topQuickWins"
    ][0]
    ui_data = with_roadmap_display_titles(
        {
            "stages": [
                {
                    "opportunities": [opportunity],
                    "requiredPermissions": [],
                }
            ]
        }
    )["stages"][0]["opportunities"][0]

    for rendered in (finding, report, ui_data):
        shown = rendered["runbook_composite"]
        assert shown["runbook_state"] == MATCH_PROPOSED
        assert shown["runbook_label"] == LABEL_PROPOSED
        assert shown["runbook_match"]["match_state"] == MATCH_PROPOSED
        assert shown["runbook_match"]["origin"] == MATCH_PROPOSED
        assert shown["runbook_match"]["label"] == LABEL_PROPOSED
        assert shown["runbook_match"]["match_state"] not in {
            MATCH_OBSERVED,
            MATCH_CONFIRMED,
        }


def test_ac3_below_threshold_emits_no_match_and_configuration_changes_outcome():
    rec = _recurrence(count=5)
    candidate = RunbookCandidate.from_chunk(_Chunk(similarity=0.72))
    strict = RunbookScoringConfig(
        match_threshold=0.80, structured_agreement_weight=0.0
    )
    calibrated = RunbookScoringConfig(
        match_threshold=0.70, structured_agreement_weight=0.0
    )

    assert propose_runbook_match("org-a", rec, [candidate], config=strict) is None
    match = propose_runbook_match("org-a", rec, [candidate], config=calibrated)
    assert match is not None
    assert match.match_state == MATCH_PROPOSED
    assert match.match_confidence == 0.72


def test_ac4_accept_dismiss_and_defer_preserve_history_and_learning_feedback():
    rec = _recurrence(count=5)
    retrieval, _ = _semantic_result(rec)
    proposal = propose_runbook_match("org-a", rec, retrieval.candidates)
    store = InMemoryRunbookMatchDecisionStore()
    store.register_match(proposal)

    deferred = store.decide("org-a", rec.record_id, ACTION_DEFER, "analyst-1")
    assert deferred.current_state == MATCH_PROPOSED
    assert deferred.current_match.match_state == MATCH_PROPOSED
    assert store.feedback("org-a") == []

    accepted = store.decide("org-a", rec.record_id, ACTION_ACCEPT, "analyst-1")
    assert accepted.current_state == MATCH_CONFIRMED
    assert accepted.current_match.match_state == MATCH_CONFIRMED

    dismissed = store.decide("org-a", rec.record_id, ACTION_DISMISS, "analyst-2")
    assert dismissed.current_state == "absent"
    assert dismissed.current_match is None
    assert store.current("org-a", rec.record_id)["current_match"] is None

    assert [event["action"] for event in store.history("org-a", rec.record_id)] == [
        ACTION_DISMISS,
        ACTION_ACCEPT,
        ACTION_DEFER,
    ]
    assert [event["feedback_label"] for event in store.feedback("org-a")] == [
        FEEDBACK_ACCEPTED,
        FEEDBACK_DISMISSED,
    ]


def test_ac5_b6_composite_covers_observed_proposed_confirmed_absent_and_unavailable():
    observed_rec = _recurrence(count=5, runbook_refs=(_RUNBOOK_ID,))
    observed = resolve_runbook_citations(
        "org-a", observed_rec, InMemoryRunbookLibrary([_page()])
    ).match
    proposed_rec = _recurrence(count=5)
    retrieval, _ = _semantic_result(proposed_rec)
    proposed = propose_runbook_match(
        "org-a", proposed_rec, retrieval.candidates
    )
    store = InMemoryRunbookMatchDecisionStore()

    observed_composite = build_documented_repeated_manual_composite(
        "org-a", observed_rec, runbook_match=observed
    )
    proposed_composite = build_current_runbook_composite(
        "org-a",
        proposed_rec,
        runbook_match=proposed,
        retrieval_status=RETRIEVAL_OK,
        store=store,
    )
    store.decide("org-a", proposed_rec.record_id, ACTION_ACCEPT, "analyst-1")
    confirmed_composite = build_current_runbook_composite(
        "org-a",
        proposed_rec,
        runbook_match=proposed,
        retrieval_status=RETRIEVAL_OK,
        store=store,
    )
    empty_store = InMemoryRunbookMatchDecisionStore()
    absent_composite = build_current_runbook_composite(
        "org-a",
        proposed_rec,
        runbook_match=None,
        retrieval_status=RETRIEVAL_OK,
        store=empty_store,
    )
    unavailable_composite = build_current_runbook_composite(
        "org-a",
        proposed_rec,
        runbook_match=None,
        retrieval_status=RETRIEVAL_UNAVAILABLE,
        store=empty_store,
    )

    assert (observed_composite.runbook_state, observed_composite.runbook_label) == (
        MATCH_OBSERVED,
        LABEL_OBSERVED,
    )
    assert (proposed_composite.runbook_state, proposed_composite.runbook_label) == (
        MATCH_PROPOSED,
        LABEL_PROPOSED,
    )
    assert (
        confirmed_composite.runbook_state,
        confirmed_composite.runbook_label,
    ) == (MATCH_CONFIRMED, LABEL_CONFIRMED)
    assert (absent_composite.runbook_state, absent_composite.runbook_label) == (
        "absent",
        LABEL_ABSENT,
    )
    assert (
        unavailable_composite.runbook_state,
        unavailable_composite.runbook_label,
    ) == ("unavailable", LABEL_UNAVAILABLE)
    assert unavailable_composite.composite_status == "degraded"
    assert unavailable_composite.ranking_treatment == "unknown_no_penalty"


def test_ac6_high_frequency_no_match_emits_capped_gap_with_loop_evidence():
    rec = _recurrence(count=6)
    retrieval = RunbookRetrievalResult(
        status=RETRIEVAL_OK,
        query="software solved permanently server platform operations",
        candidates=(),
    )
    result = evaluate_documentation_gap(
        "org-a",
        rec,
        retrieval_result=retrieval,
        citation_library=InMemoryRunbookLibrary(),
        config=DocumentationGapConfig(recurrence_floor=5, confidence_cap=0.58),
    )

    assert result.state == EVALUATION_GAP
    finding = result.finding
    assert finding.recurrence_count == 6
    assert finding.confidence == finding.confidence_cap == 0.58
    assert "resolution loop" in finding.title.casefold()
    assert "6 times" in finding.explanation.casefold()
    assert "without finding corresponding documentation" in (
        finding.explanation.casefold()
    )
    assert tuple(finding.incident_evidence) == rec.example_evidence_pointers
    assert (
        finding.search_outcome["explicit_citation"]["status"]
        == CITATION_RESOLUTION_OK
    )
    assert finding.search_outcome["semantic_retrieval"]["status"] == RETRIEVAL_OK


def test_ac7_only_redacted_text_reaches_org_scoped_retrieval_and_tenants_stay_isolated():
    secret = "AKIAIOSFODNN7EXAMPLE"
    redacted = scan_and_redact(
        f"Restarted the message service after rotating {secret}."
    )
    assert secret not in redacted.text

    rec_a = _recurrence(org_id="org-a", count=5)
    retrieve = _Retrieve([])
    result = retrieve_runbook_candidates(
        "org-a",
        rec_a,
        redacted_texts=[redacted.text],
        retrieve_fn=retrieve,
        embedding_available_fn=lambda query: True,
    )
    assert result.status == RETRIEVAL_OK
    assert retrieve.calls[0]["org_id"] == "org-a"
    assert secret not in retrieve.calls[0]["query_text"]
    assert "[REDACTED:aws_access_key_id]" in retrieve.calls[0]["query_text"]

    rec_a_cited = _recurrence(
        org_id="org-a", count=5, runbook_refs=(_RUNBOOK_ID,)
    )
    rec_b_cited = _recurrence(
        org_id="org-b", count=5, runbook_refs=(_RUNBOOK_ID,)
    )
    library = InMemoryRunbookLibrary(
        [
            _page(org_id="org-a", artifact="runbooks/org-a"),
            _page(org_id="org-b", artifact="runbooks/org-b"),
        ]
    )
    match_a = resolve_runbook_citations("org-a", rec_a_cited, library).match
    match_b = resolve_runbook_citations("org-b", rec_b_cited, library).match
    assert match_a.runbook["source_artifact"] == "runbooks/org-a"
    assert match_b.runbook["source_artifact"] == "runbooks/org-b"
    with pytest.raises(OrgScopeError):
        retrieve_runbook_candidates(
            "org-b",
            rec_a,
            retrieve_fn=retrieve,
            embedding_available_fn=lambda query: True,
        )


def test_unavailable_retrieval_degrades_b6_and_never_creates_a_false_gap():
    rec = _recurrence(count=6)
    failed_retrieve = _Retrieve(error=RuntimeError("retrieval unavailable"))
    retrieval = retrieve_runbook_candidates(
        "org-a",
        rec,
        retrieve_fn=failed_retrieve,
        embedding_available_fn=lambda query: False,
    )
    assert retrieval.status == RETRIEVAL_UNAVAILABLE

    gap = evaluate_documentation_gap(
        "org-a",
        rec,
        retrieval_result=retrieval,
        citation_library=InMemoryRunbookLibrary(),
        config=DocumentationGapConfig(recurrence_floor=5),
    )
    composite = build_documented_repeated_manual_composite(
        "org-a", rec, runbook_match=None, retrieval_status=retrieval.status
    )
    assert gap.state == EVALUATION_UNAVAILABLE
    assert gap.degraded is True
    assert gap.finding is None
    assert composite.runbook_state == "unavailable"
    assert composite.runbook_label == LABEL_UNAVAILABLE
    assert composite.composite_status == "degraded"


def test_production_pipeline_resolves_citation_before_semantic_retrieval():
    rec = _recurrence(count=5, runbook_refs=(_RUNBOOK_ID,))

    def retrieval_must_not_run(*args, **kwargs):
        raise AssertionError("semantic retrieval ran after an observed citation")

    result = evaluate_runbook_recurrence(
        "org-a",
        rec,
        citation_library=InMemoryRunbookLibrary([_page()]),
        retrieve_fn=retrieval_must_not_run,
        decision_store=InMemoryRunbookMatchDecisionStore(),
    )

    assert result.state == MATCH_OBSERVED
    assert result.retrieval_performed is False
    assert result.detected_match.match_state == MATCH_OBSERVED
    assert result.documentation_gap.state == EVALUATION_MATCHED
    assert result.composite.runbook_label == LABEL_OBSERVED
    assert len(result.detected_match.citing_incident_evidence) == 5
    assert result.detected_match.runbook_evidence["source_artifact"] == (
        "runbooks/message-service"
    )


def test_production_pipeline_redacts_raw_note_and_keeps_semantic_match_proposed():
    secret = "AKIAIOSFODNN7EXAMPLE"
    rec = _recurrence(count=5)
    retrieve = _Retrieve([_Chunk()])
    result = evaluate_runbook_recurrence(
        "org-a",
        rec,
        resolution_texts=[f"Restarted service with key {secret}"],
        citation_library=InMemoryRunbookLibrary(),
        retrieve_fn=retrieve,
        embedding_available_fn=lambda query: True,
        decision_store=InMemoryRunbookMatchDecisionStore(),
    )

    assert result.state == MATCH_PROPOSED
    assert result.detected_match.match_state == MATCH_PROPOSED
    assert result.composite.runbook_label == LABEL_PROPOSED
    assert result.documentation_gap.state == EVALUATION_MATCHED
    assert result.query_redaction.notes_redacted == 1
    assert secret not in result.retrieval.query
    assert secret not in str(result.as_dict())
    assert "[REDACTED:aws_access_key_id]" in result.retrieval.query


def test_production_batch_connects_b4_note_handoff_to_b5_and_gap_output():
    secret = "AKIAIOSFODNN7EXAMPLE"
    incidents = [_incident(i) for i in range(1, 6)]
    for incident in incidents:
        incident["close_notes"] = f"Restarted message service with {secret}"
    payload = {
        "org_id": "org-a",
        "incident_metrics": {"org_id": "org-a", "incidents": incidents},
    }
    indexed = []
    retrieve = _Retrieve([])

    result = evaluate_runbook_recurrences(
        "org-a",
        payload,
        recurrence_config=RecurrenceConfig(floor=3, window_days=30, max_examples=3),
        as_of=_AS_OF,
        citation_library=InMemoryRunbookLibrary(),
        retrieve_fn=retrieve,
        embedding_available_fn=lambda query: True,
        decision_store=InMemoryRunbookMatchDecisionStore(),
        gap_config=DocumentationGapConfig(recurrence_floor=5, confidence_cap=0.60),
        note_ingest_fn=lambda org_id, artifacts: indexed.extend(artifacts),
        record_event_fn=lambda event_name, payload: None,
    )

    assert result.note_handoff["status"] == "ok"
    assert result.note_handoff["artifacts_handed_off"] == 5
    assert len(indexed) == 5
    assert all(secret not in artifact.content for artifact in indexed)
    assert len(result.recurrences) == 1
    recurrence_result = result.recurrences[0]
    assert recurrence_result.state == "absent"
    assert recurrence_result.documentation_gap.state == EVALUATION_GAP
    assert recurrence_result.documentation_gap.finding.confidence == 0.60
    assert secret not in recurrence_result.retrieval.query
    assert retrieve.calls[0]["org_id"] == "org-a"


def test_pipeline_applies_persisted_accept_and_dismiss_to_match_and_gap():
    rec = _recurrence(count=5)
    retrieve = _Retrieve([_Chunk()])
    store = InMemoryRunbookMatchDecisionStore()
    common = {
        "citation_library": InMemoryRunbookLibrary(),
        "retrieve_fn": retrieve,
        "embedding_available_fn": lambda query: True,
        "decision_store": store,
        "gap_config": DocumentationGapConfig(recurrence_floor=5),
    }

    proposed = evaluate_runbook_recurrence("org-a", rec, **common)
    assert proposed.state == MATCH_PROPOSED

    store.decide("org-a", rec.record_id, ACTION_ACCEPT, "analyst-1")
    confirmed = evaluate_runbook_recurrence("org-a", rec, **common)
    assert confirmed.state == MATCH_CONFIRMED
    assert confirmed.documentation_gap.state == EVALUATION_MATCHED
    assert confirmed.documentation_gap.reason == "active_lifecycle_runbook_match"

    store.decide("org-a", rec.record_id, ACTION_DISMISS, "analyst-1")
    dismissed = evaluate_runbook_recurrence("org-a", rec, **common)
    assert dismissed.state == "absent"
    assert dismissed.composite.runbook_match is None
    assert dismissed.documentation_gap.state == EVALUATION_GAP
    search = dismissed.documentation_gap.finding.search_outcome
    assert search["semantic_retrieval"]["proposal_dismissed"] is True


def test_pipeline_retrieval_outage_is_unavailable_and_never_a_gap():
    rec = _recurrence(count=6)
    result = evaluate_runbook_recurrence(
        "org-a",
        rec,
        citation_library=InMemoryRunbookLibrary(),
        retrieve_fn=_Retrieve(error=RuntimeError("offline")),
        embedding_available_fn=lambda query: False,
        decision_store=InMemoryRunbookMatchDecisionStore(),
        gap_config=DocumentationGapConfig(recurrence_floor=5),
    )

    assert result.state == RUNBOOK_UNAVAILABLE
    assert result.composite.composite_status == "degraded"
    assert result.documentation_gap.state == EVALUATION_UNAVAILABLE
    assert result.documentation_gap.finding is None
