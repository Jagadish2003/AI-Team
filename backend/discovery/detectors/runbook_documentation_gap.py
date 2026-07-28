"""MSP-B5 T5 — inverse finding for repeated work with no runbook.

The finding is deliberately conservative. It is emitted only when a recurrence
meets a configurable high-frequency floor and both matching paths have completed
successfully without an observed or proposed match. An unavailable citation
library or semantic retrieval path produces an explicit unavailable evaluation,
never a documentation-gap finding.
"""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from typing import Any, Dict, Mapping, Optional, Tuple

from discovery.signals.evidence_store import OrgScopeError

from .runbook_match import (
    CITATION_RESOLUTION_OK,
    CITATION_RESOLUTION_UNAVAILABLE,
    RETRIEVAL_OK,
    RETRIEVAL_UNAVAILABLE,
    CitationResolutionResult,
    RunbookLibrary,
    RunbookMatch,
    RunbookRetrievalResult,
    RunbookScoringConfig,
    propose_runbook_match,
    resolve_runbook_citations,
    score_candidate,
)

DETECTOR_ID = "OPS_RUNBOOK_DOCUMENTATION_GAP"

DEFAULT_DOCUMENTATION_GAP_FLOOR = 5
DEFAULT_DOCUMENTATION_GAP_CONFIDENCE_CAP = 0.65
DOCUMENTATION_GAP_FLOOR_ENV = "MSP_B5_DOCUMENTATION_GAP_FLOOR"
DOCUMENTATION_GAP_CONFIDENCE_CAP_ENV = "MSP_B5_DOCUMENTATION_GAP_CONFIDENCE_CAP"

EVALUATION_GAP = "documentation_gap"
EVALUATION_MATCHED = "matched"
EVALUATION_NOT_ELIGIBLE = "not_eligible"
EVALUATION_UNAVAILABLE = "unavailable"


def _require_org(org_id: Any) -> str:
    org = str(org_id).strip() if org_id is not None else ""
    if not org:
        raise OrgScopeError("org_id is required for documentation-gap evaluation")
    return org


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class DocumentationGapConfig:
    """Tunable sensitivity without changing detector logic."""

    recurrence_floor: int = DEFAULT_DOCUMENTATION_GAP_FLOOR
    confidence_cap: float = DEFAULT_DOCUMENTATION_GAP_CONFIDENCE_CAP

    def __post_init__(self) -> None:
        if isinstance(self.recurrence_floor, bool) or not isinstance(
            self.recurrence_floor, int
        ):
            raise ValueError("recurrence_floor must be an integer")
        if self.recurrence_floor < 2:
            raise ValueError("recurrence_floor must be at least 2")
        if isinstance(self.confidence_cap, bool) or not isinstance(
            self.confidence_cap, (int, float)
        ):
            raise ValueError("confidence_cap must be numeric")
        if not 0.0 <= float(self.confidence_cap) <= 1.0:
            raise ValueError("confidence_cap must be between 0 and 1")

    @classmethod
    def from_env(cls) -> "DocumentationGapConfig":
        return cls(
            recurrence_floor=_env_int(
                DOCUMENTATION_GAP_FLOOR_ENV, DEFAULT_DOCUMENTATION_GAP_FLOOR
            ),
            confidence_cap=_env_float(
                DOCUMENTATION_GAP_CONFIDENCE_CAP_ENV,
                DEFAULT_DOCUMENTATION_GAP_CONFIDENCE_CAP,
            ),
        )


@dataclass(frozen=True)
class DocumentationGapFinding:
    """One high-volume operational loop with no corresponding runbook."""

    finding_id: str
    detector_id: str
    finding_type: str
    org_id: str
    recurrence_id: str
    title: str
    explanation: str
    loop_name: str
    recurrence_count: int
    recurrence_floor: int
    confidence: float
    confidence_cap: float
    origin: str
    evaluated_window: Dict[str, Any]
    grouped_signatures: Dict[str, str]
    incident_evidence: Tuple[Dict[str, Any], ...]
    search_outcome: Dict[str, Any]

    def as_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["incident_evidence"] = [
            dict(pointer) for pointer in self.incident_evidence
        ]
        return payload


@dataclass(frozen=True)
class DocumentationGapEvaluation:
    """Finding-or-state result; unavailability is visible and non-green."""

    org_id: str
    recurrence_id: str
    state: str
    reason: str
    degraded: bool
    finding: Optional[DocumentationGapFinding] = None
    runbook_match: Optional[RunbookMatch] = None
    citation_resolution: Optional[CitationResolutionResult] = None
    retrieval_status: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "org_id": self.org_id,
            "recurrence_id": self.recurrence_id,
            "state": self.state,
            "reason": self.reason,
            "degraded": self.degraded,
            "finding": self.finding.as_dict() if self.finding else None,
            "runbook_match": self.runbook_match.as_dict() if self.runbook_match else None,
            "citation_resolution": (
                self.citation_resolution.as_dict()
                if self.citation_resolution is not None
                else None
            ),
            "retrieval_status": self.retrieval_status,
        }


def _display_token(value: Any) -> str:
    text = " ".join(str(value or "").strip().split()).casefold()
    return text.replace("_", " ").replace("-", " ")


def _loop_name(rec: Any) -> str:
    """Name a loop from non-person structured fields only."""
    components = getattr(rec, "signature_components", {}) or {}
    identity = components.get("incident_identity", {})
    if not isinstance(identity, Mapping):
        identity = {}
    category = _display_token(identity.get("category"))
    ci_component = str(identity.get("ci_component") or "").strip()
    ci_class = ""
    if ci_component.startswith("class:"):
        ci_class = ci_component[len("class:") :]
        if ci_class.startswith("cmdb_ci_"):
            ci_class = ci_class[len("cmdb_ci_") :]
        ci_class = _display_token(ci_class)
    parts = [part for part in (category, ci_class) if part]
    return f"{' '.join(parts)} resolution loop" if parts else "repeated resolution loop"


def _validate_match(org: str, recurrence_id: str, match: RunbookMatch) -> None:
    if match.org_id != org or match.recurrence_id != recurrence_id:
        raise OrgScopeError("runbook match does not belong to this org and recurrence")


def _search_outcome(
    citation: CitationResolutionResult,
    retrieval: RunbookRetrievalResult,
    rec: Any,
    scoring_config: RunbookScoringConfig,
    *,
    proposal_dismissed: bool = False,
) -> Dict[str, Any]:
    semantic = retrieval.as_dict()
    semantic["candidate_count"] = len(retrieval.candidates)
    semantic["match_found"] = False
    semantic["proposal_dismissed"] = proposal_dismissed
    semantic["match_threshold"] = scoring_config.match_threshold
    semantic["evaluated_candidates"] = [
        {
            "source_system": candidate.source_system,
            "source_artifact": candidate.source_artifact,
            "chunk_id": candidate.chunk_id,
            "retrieval_result_id": candidate.retrieval_result_id,
            "match_score": score_candidate(rec, candidate, scoring_config),
        }
        for candidate in retrieval.candidates
    ]
    return {
        "explicit_citation": {
            "status": citation.status,
            "checked_references": list(citation.checked_references),
            "match_found": False,
            "reason": citation.reason,
        },
        "semantic_retrieval": semantic,
    }


def evaluate_documentation_gap(
    org_id: str,
    rec: Any,
    *,
    retrieval_result: RunbookRetrievalResult,
    citation_library: Optional[RunbookLibrary] = None,
    scoring_config: Optional[RunbookScoringConfig] = None,
    config: Optional[DocumentationGapConfig] = None,
    effective_match: Optional[RunbookMatch] = None,
    proposal_dismissed: bool = False,
) -> DocumentationGapEvaluation:
    """Evaluate the inverse finding after both matching paths finish.

    Explicit citations are resolved here with availability preserved. Semantic
    candidates are scored here as well, so a caller cannot accidentally treat an
    unscored candidate set as a completed no-match search. ``effective_match`` and
    ``proposal_dismissed`` are the lifecycle integration seam: an accepted match
    prevents a contradictory gap, while a dismissed proposal is no longer treated
    as active documentation.
    """
    org = _require_org(org_id)
    rec_org = str(getattr(rec, "org_id", "") or "").strip()
    if rec_org and rec_org != org:
        raise OrgScopeError(
            f"recurrence belongs to org {rec_org!r}, cannot evaluate under {org!r}"
        )
    recurrence_id = str(getattr(rec, "record_id", "") or "").strip()
    if not recurrence_id:
        raise ValueError("recurrence_id is required for documentation-gap evaluation")
    if retrieval_result.status not in {RETRIEVAL_OK, RETRIEVAL_UNAVAILABLE}:
        raise ValueError(f"invalid runbook retrieval status: {retrieval_result.status!r}")

    cfg = config or DocumentationGapConfig.from_env()
    citation = resolve_runbook_citations(org, rec, citation_library)
    if citation.status not in {
        CITATION_RESOLUTION_OK,
        CITATION_RESOLUTION_UNAVAILABLE,
    }:
        raise ValueError(f"invalid citation resolution status: {citation.status!r}")

    if citation.match is not None:
        _validate_match(org, recurrence_id, citation.match)
        return DocumentationGapEvaluation(
            org_id=org,
            recurrence_id=recurrence_id,
            state=EVALUATION_MATCHED,
            reason="explicit_runbook_match",
            degraded=False,
            runbook_match=citation.match,
            citation_resolution=citation,
            retrieval_status=retrieval_result.status,
        )

    if effective_match is not None:
        _validate_match(org, recurrence_id, effective_match)
        return DocumentationGapEvaluation(
            org_id=org,
            recurrence_id=recurrence_id,
            state=EVALUATION_MATCHED,
            reason="active_lifecycle_runbook_match",
            degraded=False,
            runbook_match=effective_match,
            citation_resolution=citation,
            retrieval_status=retrieval_result.status,
        )

    if citation.status == CITATION_RESOLUTION_UNAVAILABLE:
        return DocumentationGapEvaluation(
            org_id=org,
            recurrence_id=recurrence_id,
            state=EVALUATION_UNAVAILABLE,
            reason="explicit_citation_resolution_unavailable",
            degraded=True,
            citation_resolution=citation,
            retrieval_status=retrieval_result.status,
        )

    if retrieval_result.status == RETRIEVAL_UNAVAILABLE:
        return DocumentationGapEvaluation(
            org_id=org,
            recurrence_id=recurrence_id,
            state=EVALUATION_UNAVAILABLE,
            reason="semantic_runbook_retrieval_unavailable",
            degraded=True,
            citation_resolution=citation,
            retrieval_status=retrieval_result.status,
        )

    resolved_scoring_config = scoring_config or RunbookScoringConfig.from_env()
    semantic_match = propose_runbook_match(
        org,
        rec,
        retrieval_result.candidates,
        config=resolved_scoring_config,
    )
    if semantic_match is not None and not proposal_dismissed:
        _validate_match(org, recurrence_id, semantic_match)
        return DocumentationGapEvaluation(
            org_id=org,
            recurrence_id=recurrence_id,
            state=EVALUATION_MATCHED,
            reason="semantic_runbook_match_proposed",
            degraded=False,
            runbook_match=semantic_match,
            citation_resolution=citation,
            retrieval_status=retrieval_result.status,
        )

    recurrence_count = int(getattr(rec, "recurrence_count", 0) or 0)
    if recurrence_count < cfg.recurrence_floor:
        return DocumentationGapEvaluation(
            org_id=org,
            recurrence_id=recurrence_id,
            state=EVALUATION_NOT_ELIGIBLE,
            reason="below_high_frequency_floor",
            degraded=False,
            citation_resolution=citation,
            retrieval_status=retrieval_result.status,
        )

    loop_name = _loop_name(rec)
    window = dict(getattr(rec, "evaluated_window", {}) or {})
    window_days = window.get("days")
    window_phrase = f" in the evaluated {window_days}-day window" if window_days else ""
    confidence_cap = float(cfg.confidence_cap)
    finding = DocumentationGapFinding(
        finding_id=f"runbook-documentation-gap:{recurrence_id}",
        detector_id=DETECTOR_ID,
        finding_type="runbook_documentation_gap",
        org_id=org,
        recurrence_id=recurrence_id,
        title=f"Missing runbook for repeated {loop_name}",
        explanation=(
            f"This {loop_name} was resolved {recurrence_count} times{window_phrase}, "
            "but the explicit citation check and runbook search completed without "
            "finding corresponding documentation. Review this repeated work as a "
            "starting point for documentation and automation."
        ),
        loop_name=loop_name,
        recurrence_count=recurrence_count,
        recurrence_floor=cfg.recurrence_floor,
        confidence=confidence_cap,
        confidence_cap=confidence_cap,
        origin="inferred",
        evaluated_window=window,
        grouped_signatures=dict(getattr(rec, "grouped_signatures", {}) or {}),
        incident_evidence=tuple(
            dict(pointer)
            for pointer in (getattr(rec, "example_evidence_pointers", ()) or ())
            if isinstance(pointer, Mapping)
        ),
        search_outcome=_search_outcome(
            citation,
            retrieval_result,
            rec,
            resolved_scoring_config,
            proposal_dismissed=proposal_dismissed,
        ),
    )
    return DocumentationGapEvaluation(
        org_id=org,
        recurrence_id=recurrence_id,
        state=EVALUATION_GAP,
        reason="high_frequency_no_runbook_match",
        degraded=False,
        finding=finding,
        citation_resolution=citation,
        retrieval_status=retrieval_result.status,
    )


__all__ = [
    "DEFAULT_DOCUMENTATION_GAP_CONFIDENCE_CAP",
    "DEFAULT_DOCUMENTATION_GAP_FLOOR",
    "DETECTOR_ID",
    "DOCUMENTATION_GAP_CONFIDENCE_CAP_ENV",
    "DOCUMENTATION_GAP_FLOOR_ENV",
    "DocumentationGapConfig",
    "DocumentationGapEvaluation",
    "DocumentationGapFinding",
    "EVALUATION_GAP",
    "EVALUATION_MATCHED",
    "EVALUATION_NOT_ELIGIBLE",
    "EVALUATION_UNAVAILABLE",
    "evaluate_documentation_gap",
]
