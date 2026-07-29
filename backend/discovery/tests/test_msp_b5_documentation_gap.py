"""MSP-B5 T5 / AC6 documentation-gap finding contract tests."""
from __future__ import annotations

import os

import pytest

os.environ["INGEST_MODE"] = "offline"

from app.provenance import EvidencePointer  # noqa: E402
from discovery.detectors.ops_recurrence import (  # noqa: E402
    RecurrenceConfig,
    find_recurrences,
)
from discovery.detectors.runbook_documentation_gap import (  # noqa: E402
    EVALUATION_GAP,
    EVALUATION_MATCHED,
    EVALUATION_NOT_ELIGIBLE,
    EVALUATION_UNAVAILABLE,
    DocumentationGapConfig,
    evaluate_documentation_gap,
)
from discovery.detectors.runbook_match import (  # noqa: E402
    CITATION_RESOLUTION_UNAVAILABLE,
    MATCH_OBSERVED,
    RETRIEVAL_OK,
    RETRIEVAL_UNAVAILABLE,
    InMemoryRunbookLibrary,
    RunbookCandidate,
    RunbookLibrary,
    RunbookPage,
    RunbookRetrievalResult,
)
from discovery.signals.evidence_store import OrgScopeError  # noqa: E402
from discovery.signals.resolution_signature import (  # noqa: E402
    compute_incident_identity_signature,
    compute_resolution_signature,
)

_AS_OF = "2026-07-20 12:00:00"


def _incident(number: int, *, org_id: str, runbook_refs: tuple = ()) -> dict:
    resolved_at = f"2026-07-{10 + number:02d} 12:00:00"
    sys_id = f"incident-{number:03d}"
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
        "close_code": close_code,
        "resolved_at": resolved_at,
        # These person fields are deliberately present upstream. The gap title
        # and explanation must never copy them.
        "assigned_to": "Taylor Employee",
        "resolved_by": "Morgan Engineer",
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
            "runbook_references": list(runbook_refs),
        },
    }


def _recurrence(
    *, count: int = 5, org_id: str = "org-a", runbook_refs: tuple = ()
):
    incidents = [
        _incident(number, org_id=org_id, runbook_refs=runbook_refs)
        for number in range(1, count + 1)
    ]
    records = find_recurrences(
        {"org_id": org_id, "incident_metrics": {"org_id": org_id, "incidents": incidents}},
        config=RecurrenceConfig(floor=2, window_days=30, max_examples=3),
        as_of=_AS_OF,
        org_id=org_id,
    )
    assert len(records) == 1
    return records[0]


def _no_match_result(*candidates: RunbookCandidate) -> RunbookRetrievalResult:
    return RunbookRetrievalResult(
        status=RETRIEVAL_OK,
        query="software solved permanently server platform operations",
        candidates=tuple(candidates),
    )


def _candidate(similarity: float = 0.9) -> RunbookCandidate:
    return RunbookCandidate(
        source_system="confluence",
        source_artifact="page-42",
        content="software server platform operations solved permanently",
        similarity=similarity,
        chunk_id="chunk-42",
        retrieval_result_id="result-42",
        source_timestamp="2026-07-01T00:00:00+00:00",
    )


class _UnavailableLibrary(RunbookLibrary):
    def resolve(self, org_id: str, normalized_ref: str):
        return ()

    def resolve_checked(self, org_id: str, normalized_ref: str):
        return CITATION_RESOLUTION_UNAVAILABLE, ()


def test_high_frequency_successful_no_match_emits_gap_with_evidence_and_search_outcome():
    rec = _recurrence(count=6)
    evaluation = evaluate_documentation_gap(
        "org-a",
        rec,
        retrieval_result=_no_match_result(),
        citation_library=InMemoryRunbookLibrary(),
        config=DocumentationGapConfig(recurrence_floor=5, confidence_cap=0.6),
    )

    assert evaluation.state == EVALUATION_GAP
    assert evaluation.degraded is False
    finding = evaluation.finding.as_dict()
    assert finding["recurrence_count"] == 6
    assert finding["recurrence_floor"] == 5
    assert finding["confidence"] == finding["confidence_cap"] == 0.6
    assert [p["source_artifact"] for p in finding["incident_evidence"]] == [
        "incident-001",
        "incident-002",
        "incident-003",
    ]
    assert finding["search_outcome"]["explicit_citation"] == {
        "status": "ok",
        "checked_references": [],
        "match_found": False,
        "reason": "no_explicit_citation",
    }
    assert finding["search_outcome"]["semantic_retrieval"]["status"] == "ok"
    assert finding["search_outcome"]["semantic_retrieval"]["candidate_count"] == 0


def test_gap_wording_names_the_loop_volume_and_missing_documentation_without_people():
    finding = evaluate_documentation_gap(
        "org-a",
        _recurrence(count=5),
        retrieval_result=_no_match_result(),
        citation_library=InMemoryRunbookLibrary(),
    ).finding
    wording = f"{finding.title} {finding.explanation}".casefold()
    assert "software server resolution loop" in wording
    assert "5 times" in wording
    assert "without finding corresponding documentation" in wording
    assert "taylor employee" not in wording
    assert "morgan engineer" not in wording


def test_low_volume_recurrence_does_not_emit_an_organizational_gap():
    evaluation = evaluate_documentation_gap(
        "org-a",
        _recurrence(count=3),
        retrieval_result=_no_match_result(),
        citation_library=InMemoryRunbookLibrary(),
        config=DocumentationGapConfig(recurrence_floor=5),
    )
    assert evaluation.state == EVALUATION_NOT_ELIGIBLE
    assert evaluation.finding is None


def test_floor_and_confidence_cap_are_configurable(monkeypatch):
    monkeypatch.setenv("MSP_B5_DOCUMENTATION_GAP_FLOOR", "4")
    monkeypatch.setenv("MSP_B5_DOCUMENTATION_GAP_CONFIDENCE_CAP", "0.51")
    config = DocumentationGapConfig.from_env()
    evaluation = evaluate_documentation_gap(
        "org-a",
        _recurrence(count=4),
        retrieval_result=_no_match_result(),
        citation_library=InMemoryRunbookLibrary(),
        config=config,
    )
    assert evaluation.state == EVALUATION_GAP
    assert evaluation.finding.recurrence_floor == 4
    assert evaluation.finding.confidence == evaluation.finding.confidence_cap == 0.51


def test_explicit_match_suppresses_the_inverse_finding():
    rec = _recurrence(count=5, runbook_refs=("KB0012345",))
    library = InMemoryRunbookLibrary(
        [
            RunbookPage(
                org_id="org-a",
                source_system="confluence",
                source_artifact="page-42",
                identifiers=("KB0012345",),
            )
        ]
    )
    evaluation = evaluate_documentation_gap(
        "org-a", rec, retrieval_result=_no_match_result(), citation_library=library
    )
    assert evaluation.state == EVALUATION_MATCHED
    assert evaluation.runbook_match.match_state == MATCH_OBSERVED
    assert evaluation.finding is None


def test_semantic_proposal_suppresses_the_inverse_finding():
    evaluation = evaluate_documentation_gap(
        "org-a",
        _recurrence(count=5),
        retrieval_result=_no_match_result(_candidate(0.9)),
        citation_library=InMemoryRunbookLibrary(),
    )
    assert evaluation.state == EVALUATION_MATCHED
    assert evaluation.reason == "semantic_runbook_match_proposed"
    assert evaluation.finding is None


def test_below_threshold_candidate_is_recorded_but_does_not_block_a_real_gap():
    evaluation = evaluate_documentation_gap(
        "org-a",
        _recurrence(count=5),
        retrieval_result=_no_match_result(_candidate(0.2)),
        citation_library=InMemoryRunbookLibrary(),
    )
    assert evaluation.state == EVALUATION_GAP
    semantic = evaluation.finding.search_outcome["semantic_retrieval"]
    assert semantic["candidate_count"] == 1
    assert semantic["match_found"] is False
    assert semantic["match_threshold"] == 0.75
    assert semantic["candidates"][0]["retrieval_result_id"] == "result-42"
    assert semantic["evaluated_candidates"][0]["match_score"] < 0.75


def test_semantic_retrieval_outage_is_unavailable_not_a_false_gap():
    evaluation = evaluate_documentation_gap(
        "org-a",
        _recurrence(count=7),
        retrieval_result=RunbookRetrievalResult(
            status=RETRIEVAL_UNAVAILABLE, query="query", candidates=()
        ),
        citation_library=InMemoryRunbookLibrary(),
    )
    assert evaluation.state == EVALUATION_UNAVAILABLE
    assert evaluation.degraded is True
    assert evaluation.finding is None


def test_explicit_library_outage_is_unavailable_not_a_false_gap():
    evaluation = evaluate_documentation_gap(
        "org-a",
        _recurrence(count=7, runbook_refs=("KB0012345",)),
        retrieval_result=_no_match_result(),
        citation_library=_UnavailableLibrary(),
    )
    assert evaluation.state == EVALUATION_UNAVAILABLE
    assert evaluation.reason == "explicit_citation_resolution_unavailable"
    assert evaluation.finding is None


def test_output_is_deterministic():
    rec = _recurrence(count=5)
    kwargs = {
        "retrieval_result": _no_match_result(_candidate(0.2)),
        "citation_library": InMemoryRunbookLibrary(),
    }
    assert evaluate_documentation_gap("org-a", rec, **kwargs).as_dict() == (
        evaluate_documentation_gap("org-a", rec, **kwargs).as_dict()
    )


def test_cross_org_recurrence_is_rejected():
    with pytest.raises(OrgScopeError):
        evaluate_documentation_gap(
            "org-b",
            _recurrence(count=5, org_id="org-a"),
            retrieval_result=_no_match_result(),
            citation_library=InMemoryRunbookLibrary(),
        )


@pytest.mark.parametrize(
    "config",
    [
        DocumentationGapConfig(recurrence_floor=2, confidence_cap=0.0),
        DocumentationGapConfig(recurrence_floor=2, confidence_cap=1.0),
    ],
)
def test_valid_configuration_boundaries(config):
    assert config.recurrence_floor == 2


@pytest.mark.parametrize(
    "kwargs",
    [
        {"recurrence_floor": 1},
        {"confidence_cap": -0.01},
        {"confidence_cap": 1.01},
    ],
)
def test_invalid_configuration_is_rejected(kwargs):
    with pytest.raises(ValueError):
        DocumentationGapConfig(**kwargs)
