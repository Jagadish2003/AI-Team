"""MSP-B5 T4 contract: B6 composite lifecycle and presentation discipline."""
from __future__ import annotations

from types import SimpleNamespace

from app.opportunity_display import (
    with_display,
    with_exec_report_display_titles,
    with_roadmap_display_titles,
)
from app.provenance import EvidencePointer
from discovery.detectors.runbook_composite import (
    LABEL_ABSENT,
    LABEL_CONFIRMED,
    LABEL_OBSERVED,
    LABEL_PROPOSED,
    LABEL_UNAVAILABLE,
    build_documented_repeated_manual_composite,
)
from discovery.detectors.runbook_match import (
    MATCH_CONFIRMED,
    MATCH_OBSERVED,
    MATCH_PROPOSED,
    RETRIEVAL_OK,
    RETRIEVAL_UNAVAILABLE,
    RunbookMatch,
)


def _pointer(artifact: str) -> dict:
    return EvidencePointer.observed(
        source_system="servicenow",
        source_artifact=artifact,
        source_timestamp="2026-07-20T10:00:00+00:00",
        source_artifact_type="record_id",
    ).to_dict()


def _recurrence(org_id: str = "org-a"):
    return SimpleNamespace(
        org_id=org_id,
        record_id="rec-001",
        example_evidence_pointers=(_pointer("inc-1"), _pointer("inc-2")),
    )


def _match(state: str) -> RunbookMatch:
    return RunbookMatch(
        org_id="org-a",
        recurrence_id="rec-001",
        match_state=state,
        origin=state,
        runbook={
            "source_system": "confluence",
            "source_artifact": "page-42",
            "title": "Restart message broker",
            "url": "https://docs.example/runbooks/page-42",
            "identifiers": ["RB-BROKER"],
        },
        runbook_evidence=EvidencePointer.observed(
            source_system="confluence",
            source_artifact="page-42",
            source_timestamp="2026-07-01T00:00:00+00:00",
            source_artifact_type="record_id",
        ).to_dict(),
        citing_incident_evidence=(_pointer("inc-1"), _pointer("inc-2")),
        cited_references=("RB-BROKER",),
        match_confidence=0.91 if state == MATCH_PROPOSED else None,
    )


def test_observed_match_gets_strongest_full_composite_and_both_evidence_sides():
    finding = build_documented_repeated_manual_composite(
        "org-a", _recurrence(), runbook_match=_match(MATCH_OBSERVED)
    ).as_dict()
    assert finding["runbook_state"] == MATCH_OBSERVED
    assert finding["runbook_label"] == LABEL_OBSERVED
    assert finding["documented_status"] == "satisfied"
    assert finding["composite_status"] == "full"
    assert finding["ranking_treatment"] == "strongest"
    assert finding["evidence"]["runbook"]["runbook"]["source_artifact"] == "page-42"
    assert [p["source_artifact"] for p in finding["evidence"]["runbook"]["citing_incidents"]] == ["inc-1", "inc-2"]


def test_proposal_contributes_but_is_never_presented_as_fact():
    finding = build_documented_repeated_manual_composite(
        "org-a", _recurrence(), runbook_match=_match(MATCH_PROPOSED)
    ).as_dict()
    assert finding["runbook_state"] == MATCH_PROPOSED
    assert finding["runbook_label"] == LABEL_PROPOSED
    assert finding["documented_status"] == "proposed"
    assert finding["composite_status"] == "provisional"
    assert finding["ranking_treatment"] == "provisional"
    assert finding["runbook_match"]["label"] == LABEL_PROPOSED
    assert finding["runbook_match"]["match_state"] not in {MATCH_OBSERVED, MATCH_CONFIRMED}
    # Semantic retrieval is not evidence that an incident cited the page.
    assert finding["evidence"]["runbook"]["citing_incidents"] == []


def test_confirmed_match_is_full_but_keeps_analyst_confirmed_evidence_status():
    finding = build_documented_repeated_manual_composite(
        "org-a", _recurrence(), runbook_match=_match(MATCH_CONFIRMED)
    ).as_dict()
    assert finding["runbook_label"] == LABEL_CONFIRMED
    assert finding["composite_status"] == "full"
    assert finding["ranking_treatment"] == "strongest"
    assert finding["evidence"]["runbook"]["status"] == "analyst_confirmed"
    assert finding["evidence"]["runbook"]["citing_incidents"] == []


def test_absent_and_unavailable_are_distinct_and_an_outage_is_not_a_gap():
    absent = build_documented_repeated_manual_composite(
        "org-a", _recurrence(), retrieval_status=RETRIEVAL_OK
    ).as_dict()
    unavailable = build_documented_repeated_manual_composite(
        "org-a", _recurrence(), retrieval_status=RETRIEVAL_UNAVAILABLE
    ).as_dict()
    assert (absent["runbook_state"], absent["runbook_label"]) == ("absent", LABEL_ABSENT)
    assert absent["documented_status"] == "not_satisfied"
    assert (unavailable["runbook_state"], unavailable["runbook_label"]) == ("unavailable", LABEL_UNAVAILABLE)
    assert unavailable["documented_status"] == "unavailable"
    assert unavailable["ranking_treatment"] == "unknown_no_penalty"


def test_finding_report_and_demo_shapers_all_keep_the_proposed_label():
    composite = build_documented_repeated_manual_composite(
        "org-a", _recurrence(), runbook_match=_match(MATCH_PROPOSED)
    ).as_dict()
    opportunity = {
        "id": "opp-1",
        "title": "Repeated broker restart",
        "impact": 4,
        "effort": 2,
        "runbook_composite": composite,
    }

    finding = with_display(opportunity)
    report = with_exec_report_display_titles({"topQuickWins": [opportunity]})
    demo = with_roadmap_display_titles({
        "stages": [{"opportunities": [opportunity], "requiredPermissions": []}]
    })
    rendered = [
        finding,
        report["topQuickWins"][0],
        demo["stages"][0]["opportunities"][0],
    ]
    for item in rendered:
        shown = item["runbook_composite"]
        assert shown["runbook_state"] == MATCH_PROPOSED
        assert shown["runbook_label"] == LABEL_PROPOSED
        assert shown["runbook_match"]["match_state"] == MATCH_PROPOSED
        assert shown["runbook_match"]["label"] == LABEL_PROPOSED
