"""MSP-B5 T4 — runbook lifecycle in B6's documented/repeated/manual finding.

This module is deliberately pure.  B6 can call it from discovery, reporting,
or demo materialisation and receive the same presentation contract every time.
The lifecycle is explicit: a retrieval proposal can contribute to a composite,
but only an explicit citation (observed) or analyst acceptance (confirmed)
satisfies the documented leg.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from discovery.signals.evidence_store import OrgScopeError

from .runbook_match import (
    MATCH_CONFIRMED,
    MATCH_OBSERVED,
    MATCH_PROPOSED,
    RETRIEVAL_OK,
    RETRIEVAL_UNAVAILABLE,
    RunbookMatch,
)

DETECTOR_ID = "OPS_DOCUMENTED_REPEATED_MANUAL"

RUNBOOK_ABSENT = "absent"
RUNBOOK_UNAVAILABLE = "unavailable"
RUNBOOK_STATES = frozenset(
    {MATCH_OBSERVED, MATCH_PROPOSED, MATCH_CONFIRMED, RUNBOOK_ABSENT, RUNBOOK_UNAVAILABLE}
)

LABEL_OBSERVED = "Observed runbook match"
LABEL_PROPOSED = "Proposed match, pending confirmation"
LABEL_CONFIRMED = "Confirmed runbook match"
LABEL_ABSENT = "No runbook match"
LABEL_UNAVAILABLE = "Runbook matching unavailable"

_STATE_PRESENTATION: Dict[str, Dict[str, Any]] = {
    MATCH_OBSERVED: {
        "label": LABEL_OBSERVED,
        "documented_status": "satisfied",
        "composite_status": "full",
        "ranking_treatment": "strongest",
        "evidence_status": "observed",
        "active": True,
    },
    MATCH_CONFIRMED: {
        "label": LABEL_CONFIRMED,
        "documented_status": "satisfied",
        "composite_status": "full",
        "ranking_treatment": "strongest",
        "evidence_status": "analyst_confirmed",
        "active": True,
    },
    MATCH_PROPOSED: {
        "label": LABEL_PROPOSED,
        "documented_status": "proposed",
        "composite_status": "provisional",
        "ranking_treatment": "provisional",
        "evidence_status": "proposed",
        "active": True,
    },
    RUNBOOK_ABSENT: {
        "label": LABEL_ABSENT,
        "documented_status": "not_satisfied",
        "composite_status": "incomplete",
        "ranking_treatment": "no_documented_lift",
        "evidence_status": "absent",
        "active": False,
    },
    RUNBOOK_UNAVAILABLE: {
        "label": LABEL_UNAVAILABLE,
        "documented_status": "unavailable",
        "composite_status": "degraded",
        "ranking_treatment": "unknown_no_penalty",
        "evidence_status": "unavailable",
        "active": False,
    },
}


def presentation_for_state(state: str) -> Dict[str, Any]:
    """Return the one canonical user-facing treatment for a lifecycle state."""
    normalized = str(state or "").strip().lower()
    if normalized not in _STATE_PRESENTATION:
        raise ValueError(f"invalid runbook lifecycle state: {state!r}")
    return {"state": normalized, **_STATE_PRESENTATION[normalized]}


def present_runbook_match(value: Optional[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    """Normalise a stored match for findings, reports, and demonstration views.

    The state, label, and evidence status always come from the same locked map;
    downstream surfaces cannot silently relabel a proposal as confirmed.
    """
    if value is None:
        return None
    result = dict(value)
    state = str(result.get("match_state") or result.get("state") or "").lower()
    presentation = presentation_for_state(state)
    result["match_state"] = state
    result["origin"] = state if state in {MATCH_OBSERVED, MATCH_PROPOSED, MATCH_CONFIRMED} else None
    result["lifecycle"] = presentation
    result["label"] = presentation["label"]
    return result


@dataclass(frozen=True)
class DocumentedRepeatedManualFinding:
    finding_id: str
    detector_id: str
    org_id: str
    recurrence_id: str
    title: str
    explanation: str
    runbook_state: str
    runbook_label: str
    documented_status: str
    repeated_status: str
    manual_status: str
    composite_status: str
    ranking_treatment: str
    runbook_match: Optional[Dict[str, Any]]
    evidence: Dict[str, Any]

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _org(value: Any) -> str:
    result = str(value or "").strip()
    if not result:
        raise OrgScopeError("org_id is required for the runbook composite")
    return result


def _recurrence_id(recurrence: Any) -> str:
    if isinstance(recurrence, Mapping):
        return str(recurrence.get("record_id") or recurrence.get("recurrence_id") or "").strip()
    return str(getattr(recurrence, "record_id", "") or "").strip()


def _recurrence_org(recurrence: Any) -> Optional[str]:
    value = recurrence.get("org_id") if isinstance(recurrence, Mapping) else getattr(recurrence, "org_id", None)
    return str(value).strip() if value else None


def _recurrence_evidence(recurrence: Any) -> Tuple[Dict[str, Any], ...]:
    value = (
        recurrence.get("example_evidence_pointers", ())
        if isinstance(recurrence, Mapping)
        else getattr(recurrence, "example_evidence_pointers", ())
    )
    return tuple(dict(pointer) for pointer in (value or ()) if isinstance(pointer, Mapping))


def build_documented_repeated_manual_composite(
    org_id: str,
    recurrence: Any,
    *,
    runbook_match: Optional[RunbookMatch] = None,
    retrieval_status: str = RETRIEVAL_OK,
    manual: bool = True,
    manual_evidence: Sequence[Mapping[str, Any]] = (),
) -> DocumentedRepeatedManualFinding:
    """Build B6's composite with honest treatment of every runbook state.

    ``None`` means absent only when retrieval actually ran.  A retrieval outage
    is represented as unavailable, so it cannot be mistaken for a real gap.
    """
    org = _org(org_id)
    recurrence_org = _recurrence_org(recurrence)
    if recurrence_org and recurrence_org != org:
        raise OrgScopeError(
            f"recurrence belongs to org {recurrence_org!r}, cannot build under {org!r}"
        )
    recurrence_id = _recurrence_id(recurrence)
    if not recurrence_id:
        raise ValueError("recurrence_id is required for the runbook composite")

    if runbook_match is not None:
        if runbook_match.org_id != org or runbook_match.recurrence_id != recurrence_id:
            raise OrgScopeError("runbook match does not belong to this org and recurrence")
        state = runbook_match.match_state
        if state not in {MATCH_OBSERVED, MATCH_PROPOSED, MATCH_CONFIRMED}:
            raise ValueError(f"invalid active runbook match state: {state!r}")
    else:
        if retrieval_status not in {RETRIEVAL_OK, RETRIEVAL_UNAVAILABLE}:
            raise ValueError(f"invalid runbook retrieval status: {retrieval_status!r}")
        state = RUNBOOK_UNAVAILABLE if retrieval_status == RETRIEVAL_UNAVAILABLE else RUNBOOK_ABSENT

    lifecycle = presentation_for_state(state)
    match_payload = present_runbook_match(runbook_match.as_dict()) if runbook_match else None
    runbook_evidence: Dict[str, Any] = {
        "status": lifecycle["evidence_status"],
        "runbook": None,
        "citing_incidents": [],
    }
    if match_payload is not None:
        runbook_evidence["runbook"] = dict(runbook_match.runbook_evidence)
        # Explicit citations are meaningful only for the observed path.  A
        # semantic proposal never borrows recurrence incidents as if they cited it.
        if state == MATCH_OBSERVED:
            runbook_evidence["citing_incidents"] = [
                dict(pointer) for pointer in runbook_match.citing_incident_evidence
            ]

    title = "Documented, repeated manual resolution loop"
    explanation = (
        "A repeated manual resolution pattern has an established runbook."
        if state in {MATCH_OBSERVED, MATCH_CONFIRMED}
        else "A repeated manual resolution pattern has a possible runbook awaiting review."
        if state == MATCH_PROPOSED
        else "A repeated manual resolution pattern has no matched runbook."
        if state == RUNBOOK_ABSENT
        else "A repeated manual resolution pattern was found, but runbook matching was unavailable."
    )

    return DocumentedRepeatedManualFinding(
        finding_id=f"drm_{recurrence_id}",
        detector_id=DETECTOR_ID,
        org_id=org,
        recurrence_id=recurrence_id,
        title=title,
        explanation=explanation,
        runbook_state=state,
        runbook_label=lifecycle["label"],
        documented_status=lifecycle["documented_status"],
        repeated_status="satisfied",
        manual_status="satisfied" if manual else "not_satisfied",
        composite_status=(
            lifecycle["composite_status"] if manual else "incomplete"
        ),
        ranking_treatment=lifecycle["ranking_treatment"],
        runbook_match=match_payload,
        evidence={
            "recurrence": [dict(pointer) for pointer in _recurrence_evidence(recurrence)],
            "manual": [dict(pointer) for pointer in manual_evidence],
            "runbook": runbook_evidence,
        },
    )
