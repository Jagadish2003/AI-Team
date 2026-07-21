"""MSP-B5 T3 — deterministic candidate scoring + threshold => PROPOSED match.

Covers the acceptance criteria that belong to T3:

  * AC2 — a candidate that clears the threshold yields a PROPOSED match carrying
          match_confidence and the proposed label — and is never rendered as
          observed / confirmed.
  * AC3 — a below-threshold best candidate yields NO match; the threshold is
          config, not code (calibration without a code change).

Plus the task's scoring guarantees: the score preserves retrieval similarity and
rewards structured agreement; the strongest candidate is chosen with a STABLE
ordering (provenance tie-breakers) so repeated runs agree; below threshold the
weaker candidate is never selected and the threshold is never auto-lowered; and
the runbook provenance + retrieval-result ids that explain the proposal are
preserved. Pure-Python and offline.
"""
from __future__ import annotations

import os

import pytest

os.environ["INGEST_MODE"] = "offline"

from app.provenance import EvidencePointer  # noqa: E402
from discovery.detectors.ops_recurrence import (  # noqa: E402
    RecurrenceConfig,
    find_recurrences,
)
from discovery.detectors.runbook_match import (  # noqa: E402
    DEFAULT_MATCH_THRESHOLD,
    MATCH_CONFIRMED,
    MATCH_OBSERVED,
    MATCH_PROPOSED,
    RunbookCandidate,
    RunbookScoringConfig,
    propose_runbook_match,
    score_candidate,
)
from discovery.signals.evidence_store import OrgScopeError  # noqa: E402
from discovery.signals.resolution_signature import (  # noqa: E402
    compute_incident_identity_signature,
    compute_resolution_signature,
)

_AS_OF = "2026-07-15 12:00:00"
_CONFIG = RecurrenceConfig(floor=3, window_days=30, max_examples=3)

# Content that mentions the recurrence's resolution-pattern terms (full agreement).
_MATCHING_CONTENT = (
    "Runbook: software incident on cmdb_ci_server. Resolution marked solved "
    "permanently by the platform operations queue."
)
_UNRELATED_CONTENT = "This document is about office coffee machine maintenance."


def _incident(number: int, *, org_id: str = "org-a") -> dict:
    sys_id = f"incident-sys-{number:04d}"
    category, close_code = "software", "Solved (Permanently)"
    ci_class, group = "cmdb_ci_server", "Platform Operations"
    return {
        "sys_id": sys_id, "number": f"INC{number:07d}", "org_id": org_id,
        "category": category, "ci_class": ci_class,
        "short_description": "Portal email service unavailable",
        "assignment_group": group, "close_code": close_code,
        "resolved_at": f"2026-07-1{number} 12:00:00",
        "resolution": {
            "is_resolved": True, "resolution_category": category,
            "close_code": close_code, "resolved_by_group": group,
            "resolved_at": f"2026-07-1{number} 12:00:00",
            "time_to_resolve_seconds": 3600,
            "incident_identity_signature": compute_incident_identity_signature(
                category=category, short_description="Portal email service unavailable",
                ci_class=ci_class),
            "resolution_signature": compute_resolution_signature(
                category=category, close_code=close_code,
                resolved_by_group=group, ci_class=ci_class),
            "incident_sys_id": sys_id,
        },
    }


def _recurrence(org_id: str = "org-a"):
    incidents = [_incident(1, org_id=org_id), _incident(2, org_id=org_id),
                 _incident(3, org_id=org_id)]
    payload = {"org_id": org_id,
               "incident_metrics": {"org_id": org_id, "incidents": incidents}}
    records = find_recurrences(payload, config=_CONFIG, as_of=_AS_OF, org_id=org_id)
    assert len(records) == 1
    return records[0]


def _candidate(similarity: float, *, source_artifact="runbooks/a.md", chunk_id="chunk-1",
               content=_MATCHING_CONTENT, retrieval_result_id="rr-1",
               source_system="document", is_stale=False) -> RunbookCandidate:
    return RunbookCandidate(
        source_system=source_system, source_artifact=source_artifact, content=content,
        similarity=similarity, chunk_id=chunk_id, retrieval_result_id=retrieval_result_id,
        source_timestamp="2026-07-01T00:00:00+00:00", is_stale=is_stale,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Deterministic scoring.
# ─────────────────────────────────────────────────────────────────────────────


class TestScoring:
    def test_score_preserves_retrieval_similarity(self):
        rec = _recurrence()
        # With no structured agreement, the score is exactly the similarity.
        score = score_candidate(rec, _candidate(0.62, content=_UNRELATED_CONTENT))
        assert score == 0.62

    def test_structured_agreement_lifts_the_score(self):
        rec = _recurrence()
        agreeing = score_candidate(rec, _candidate(0.62, content=_MATCHING_CONTENT))
        unrelated = score_candidate(rec, _candidate(0.62, content=_UNRELATED_CONTENT))
        assert agreeing > unrelated
        assert agreeing >= 0.62  # never scores below its own similarity

    def test_score_is_deterministic(self):
        rec = _recurrence()
        cand = _candidate(0.81)
        assert score_candidate(rec, cand) == score_candidate(rec, cand)

    def test_score_is_clamped_to_unit_interval(self):
        rec = _recurrence()
        assert score_candidate(rec, _candidate(1.0, content=_MATCHING_CONTENT)) <= 1.0
        assert score_candidate(rec, _candidate(0.0, content=_UNRELATED_CONTENT)) >= 0.0


# ─────────────────────────────────────────────────────────────────────────────
# AC2 — proposed match with confidence, visibly distinct from observed/confirmed.
# ─────────────────────────────────────────────────────────────────────────────


class TestAC2ProposedMatch:
    def test_above_threshold_yields_proposed_match_with_confidence(self):
        rec = _recurrence()
        match = propose_runbook_match("org-a", rec, [_candidate(0.9)])
        assert match is not None
        assert match.match_state == MATCH_PROPOSED
        assert match.origin == "proposed"
        assert isinstance(match.match_confidence, float)
        assert match.match_confidence >= DEFAULT_MATCH_THRESHOLD

    def test_proposed_is_never_observed_or_confirmed(self):
        rec = _recurrence()
        match = propose_runbook_match("org-a", rec, [_candidate(0.9)])
        rendered = match.as_dict()
        assert rendered["match_state"] == MATCH_PROPOSED
        assert rendered["origin"] == "proposed"
        assert rendered["match_state"] != MATCH_OBSERVED
        assert rendered["match_state"] != MATCH_CONFIRMED
        # A proposal carries a confidence; an observed match never does.
        assert rendered["match_confidence"] is not None

    def test_proposal_preserves_runbook_provenance_and_retrieval_ids(self):
        rec = _recurrence()
        cand = _candidate(0.9, source_artifact="runbooks/loan-close.md",
                          chunk_id="chunk-42", retrieval_result_id="rr-99")
        match = propose_runbook_match("org-a", rec, [cand])
        assert match.runbook["source_artifact"] == "runbooks/loan-close.md"
        ptr = EvidencePointer.from_dict(match.runbook_evidence)
        assert ptr.is_valid()
        assert ptr.chunk_id == "chunk-42"
        assert ptr.retrieval_result_id == "rr-99"
        assert ptr.origin == "observed"          # retrieved content is observed...
        assert ptr.confidence == 0.9              # ...and preserves raw similarity

    def test_match_confidence_is_the_combined_score(self):
        rec = _recurrence()
        cand = _candidate(0.8, content=_MATCHING_CONTENT)
        match = propose_runbook_match("org-a", rec, [cand])
        assert match.match_confidence == score_candidate(rec, cand)


# ─────────────────────────────────────────────────────────────────────────────
# AC3 — below threshold => no match; threshold is config, not code.
# ─────────────────────────────────────────────────────────────────────────────


class TestAC3BelowThreshold:
    def test_below_threshold_yields_no_match(self):
        rec = _recurrence()
        # 0.5 similarity, unrelated content -> 0.5 < 0.75 default threshold.
        assert propose_runbook_match(
            "org-a", rec, [_candidate(0.5, content=_UNRELATED_CONTENT)]
        ) is None

    def test_weaker_candidate_is_not_selected_when_all_below_threshold(self):
        rec = _recurrence()
        candidates = [
            _candidate(0.4, source_artifact="runbooks/a.md", content=_UNRELATED_CONTENT),
            _candidate(0.6, source_artifact="runbooks/b.md", content=_UNRELATED_CONTENT),
        ]
        # Best (0.6) is still below 0.75 -> no match; the threshold is not lowered.
        assert propose_runbook_match("org-a", rec, candidates) is None

    def test_threshold_is_config_not_code(self):
        rec = _recurrence()
        cand = _candidate(0.6, content=_UNRELATED_CONTENT)  # score 0.6
        # Default threshold rejects it...
        assert propose_runbook_match("org-a", rec, [cand]) is None
        # ...a calibrated lower threshold (config, no code change) accepts it.
        cfg = RunbookScoringConfig(match_threshold=0.5)
        match = propose_runbook_match("org-a", rec, [cand], config=cfg)
        assert match is not None
        assert match.match_confidence == 0.6

    def test_threshold_from_env(self, monkeypatch):
        monkeypatch.setenv("MSP_B5_RUNBOOK_MATCH_THRESHOLD", "0.55")
        cfg = RunbookScoringConfig.from_env()
        assert cfg.match_threshold == 0.55

    def test_empty_candidates_yield_no_match(self):
        rec = _recurrence()
        assert propose_runbook_match("org-a", rec, []) is None


# ─────────────────────────────────────────────────────────────────────────────
# Stable selection + org scoping.
# ─────────────────────────────────────────────────────────────────────────────


class TestSelectionAndScoping:
    def test_strongest_candidate_is_selected(self):
        rec = _recurrence()
        weak = _candidate(0.80, source_artifact="runbooks/weak.md")
        strong = _candidate(0.95, source_artifact="runbooks/strong.md")
        match = propose_runbook_match("org-a", rec, [weak, strong])
        assert match.runbook["source_artifact"] == "runbooks/strong.md"

    def test_equal_scores_break_ties_stably_by_provenance(self):
        rec = _recurrence()
        # Identical score; tie-break must pick the lexicographically-first artifact.
        a = _candidate(0.9, source_artifact="runbooks/a.md", chunk_id="c9")
        b = _candidate(0.9, source_artifact="runbooks/b.md", chunk_id="c1")
        assert propose_runbook_match("org-a", rec, [a, b]).runbook[
            "source_artifact"] == "runbooks/a.md"
        # Order of input does not change the winner (deterministic).
        assert propose_runbook_match("org-a", rec, [b, a]).runbook[
            "source_artifact"] == "runbooks/a.md"

    def test_equal_artifact_breaks_ties_by_chunk_id(self):
        rec = _recurrence()
        a = _candidate(0.9, source_artifact="runbooks/same.md", chunk_id="chunk-002")
        b = _candidate(0.9, source_artifact="runbooks/same.md", chunk_id="chunk-001")
        match = propose_runbook_match("org-a", rec, [a, b])
        ptr = EvidencePointer.from_dict(match.runbook_evidence)
        assert ptr.chunk_id == "chunk-001"

    def test_selection_is_deterministic_across_repeated_runs(self):
        rec = _recurrence()
        candidates = [_candidate(0.9, source_artifact=f"runbooks/{c}.md", chunk_id=c)
                      for c in ("m", "a", "z", "b")]
        first = propose_runbook_match("org-a", rec, candidates).as_dict()
        second = propose_runbook_match("org-a", rec, list(reversed(candidates))).as_dict()
        assert first == second

    def test_cross_org_recurrence_raises(self):
        rec = _recurrence(org_id="org-b")
        with pytest.raises(OrgScopeError):
            propose_runbook_match("org-a", rec, [_candidate(0.9)])

    def test_missing_org_raises(self):
        rec = _recurrence(org_id="org-a")
        with pytest.raises(OrgScopeError):
            propose_runbook_match("", rec, [_candidate(0.9)])
