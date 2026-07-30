from __future__ import annotations

from typing import Any, Dict, List, Optional, Set

from .pack_state import (
    DISABLED_PACK_LABEL,
    STATE_ACTIVE as PACK_STATE_ACTIVE,
    STATE_DISABLED as PACK_STATE_DISABLED,
)
from .roadmap_engine import overall_readiness, uniq_permissions_merge

from discovery.detectors.runbook_composite import (
    present_runbook_match,
    presentation_for_state,
)

try:
    from discovery.track_a_adapter import get_required_permissions_for_detector
except ModuleNotFoundError:  # project-root execution uses backend as package
    from backend.discovery.track_a_adapter import get_required_permissions_for_detector


OPPORTUNITY_TITLE_OVERRIDES = {
    "APPLICATION_STALL": "Retirement Application Monitor",
    "BENEFIT_ELECTION_DEADLINE": "Benefit Election Guardian",
}

LEGACY_OPPORTUNITY_TITLE_OVERRIDES = {
    "Application Stall": "Retirement Application Monitor",
    "Benefit Election Deadline": "Benefit Election Guardian",
}


def with_display_title(
    opp: Dict[str, Any], disabled_pack_ids: Optional[Set[str]] = None
) -> Dict[str, Any]:
    """Shape one opportunity for display.

    ``disabled_pack_ids`` is the caller's pre-resolved set of this org's disabled
    packs (2.0-C1 T2). Pass it when shaping a LIST so the pack state is read once
    for the whole list instead of once per finding; omit it for a single
    opportunity and it is resolved here.
    """
    display_opp = dict(opp)
    debug = display_opp.get("_debug") or {}
    detector_id = str(debug.get("detector_id") or display_opp.get("detector_id") or "")
    title = str(display_opp.get("title") or "")
    display_title = (
        OPPORTUNITY_TITLE_OVERRIDES.get(detector_id)
        or LEGACY_OPPORTUNITY_TITLE_OVERRIDES.get(title)
    )
    if display_title:
        display_opp["title"] = display_title
    return with_pack_state(
        with_runbook_lifecycle(display_opp), disabled_pack_ids=disabled_pack_ids
    )


def _resolve_disabled_pack_ids() -> Set[str]:
    """This org's disabled packs, resolved from the request's tenancy context.

    Fail-soft on every axis (no request context, no DB, store error ⇒ empty set):
    a historical finding must stay retrievable and viewable even when pack state
    cannot be read, so the LABEL degrades before the finding does (2.0-C1 AC2).
    """
    try:
        from .middleware.tenancy import get_current_org_id_optional
        from .pack_state import disabled_pack_ids_safe

        return disabled_pack_ids_safe(get_current_org_id_optional())
    except Exception:  # noqa: BLE001
        return set()


def with_pack_state(
    opp: Dict[str, Any], disabled_pack_ids: Optional[Set[str]] = None
) -> Dict[str, Any]:
    """Mark a finding whose producing pack is disabled TODAY (2.0-C1 T2 / AC2).

    Disabling a pack never removes or rewrites its historical findings — they stay
    exactly as produced. What changes is that a reader must be able to tell that
    the finding came from a pack that is no longer running, so this stamps two
    ADDITIVE fields (the ``connector_roadmap.annotate_connector`` pattern):

        packState      : "active" | "disabled"
        packStateLabel : the disabled label, or absent when active

    Nothing else on the finding is touched — not the score, not the evidence, not
    the pack version stamp. A finding with no ``packId`` (pre-R16-B1) is returned
    unchanged rather than guessed at.
    """
    pack_id = str(opp.get("packId") or "").strip()
    if not pack_id:
        return opp
    disabled = (
        _resolve_disabled_pack_ids() if disabled_pack_ids is None else disabled_pack_ids
    )
    if pack_id not in disabled:
        # Absence of a row means active — state the fact rather than leaving the
        # field missing, so a consumer never has to infer it.
        return {**opp, "packState": PACK_STATE_ACTIVE}
    return {
        **opp,
        "packState": PACK_STATE_DISABLED,
        "packStateLabel": DISABLED_PACK_LABEL,
    }


def with_pack_states(opps: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Apply :func:`with_pack_state` to a list, reading pack state ONCE."""
    disabled = _resolve_disabled_pack_ids()
    return [with_pack_state(opp, disabled_pack_ids=disabled) for opp in opps]


def with_runbook_lifecycle(opp: Dict[str, Any]) -> Dict[str, Any]:
    """Apply one lifecycle label to finding, report, and demo opportunity data.

    B6 may be carried directly as ``runbook_match`` or nested under
    ``runbook_composite``.  Both shapes are normalised from the authoritative
    state map, so no display path can quietly turn ``proposed`` into
    ``confirmed``.
    """
    result = dict(opp)
    for key in ("runbook_match", "runbookMatch"):
        value = result.get(key)
        if isinstance(value, dict):
            result[key] = present_runbook_match(value)

    for key in ("runbook_composite", "runbookComposite"):
        value = result.get(key)
        if not isinstance(value, dict):
            continue
        composite = dict(value)
        state = composite.get("runbook_state") or composite.get("runbookState")
        if state:
            lifecycle = presentation_for_state(str(state))
            composite["runbook_label"] = lifecycle["label"]
            composite["runbook_lifecycle"] = lifecycle
        nested = composite.get("runbook_match")
        if isinstance(nested, dict):
            composite["runbook_match"] = present_runbook_match(nested)
        result[key] = composite
    return result


def with_display_titles(opps: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    # Pack state is read ONCE for the whole list and threaded down, so a 200-finding
    # response costs one state read rather than 200.
    disabled = _resolve_disabled_pack_ids()
    return [with_display_title(opp, disabled_pack_ids=disabled) for opp in opps]


def with_display_scores(opp: Dict[str, Any]) -> Dict[str, Any]:
    """Apply the stable, display-only impact/effort offset for the Effort-vs-Impact matrix.

    Opportunities that share identical raw impact/effort scores would stack on a
    single point in the quadrant chart, so a small deterministic per-id offset
    spreads them. Because it is keyed off the opportunity id, the SAME opportunity
    always lands at the SAME coordinates.

    This is the reason every endpoint that returns an opportunity to the matrix
    (list, decision, override) MUST apply this: the matrix positions a bubble from
    impact/effort, so if the decision/override response returned RAW scores while
    the list returned OFFSET scores, the bubble would visibly jump the moment its
    decision changed. Returns a copy; the raw stored scores are never mutated.
    """
    if "impact" not in opp or "effort" not in opp:
        return dict(opp)
    _id = str(opp.get("id", "0"))
    stable_offset = (sum(ord(c) for c in _id) % 5) * 0.15
    display_opp = dict(opp)
    display_opp["impact"] = float(opp["impact"]) + stable_offset
    display_opp["effort"] = float(opp["effort"]) + stable_offset
    return display_opp


def with_display(
    opp: Dict[str, Any], disabled_pack_ids: Optional[Set[str]] = None
) -> Dict[str, Any]:
    """Full single-opportunity display shaping: title overrides + the stable matrix
    score offset + the pack-state label. Use at every opportunity return site so
    list/decision/override responses are coordinate-consistent.

    For a LIST of opportunities use :func:`with_display_all`, which reads pack state
    once instead of once per finding."""
    return with_display_scores(
        with_display_title(opp, disabled_pack_ids=disabled_pack_ids)
    )


def with_display_all(opps: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """:func:`with_display` over a list, reading this org's pack state ONCE."""
    disabled = _resolve_disabled_pack_ids()
    return [with_display(opp, disabled_pack_ids=disabled) for opp in opps]


def with_roadmap_display_titles(roadmap: Dict[str, Any]) -> Dict[str, Any]:
    display_roadmap = dict(roadmap)
    stages = display_roadmap.get("stages")
    if isinstance(stages, list):
        normalized_stages = []
        for stage in stages:
            if not isinstance(stage, dict):
                normalized_stages.append(stage)
                continue

            labels = []
            for permission in stage.get("requiredPermissions") or []:
                if isinstance(permission, dict):
                    label = str(permission.get("label") or "").strip()
                    if label:
                        labels.append(label)
                elif isinstance(permission, str):
                    label = permission.strip()
                    if label:
                        labels.append(label)

            for opportunity in stage.get("opportunities") or []:
                if not isinstance(opportunity, dict):
                    continue

                opp_permissions = opportunity.get("requiredPermissions") or []
                if opp_permissions:
                    for permission in opp_permissions:
                        if isinstance(permission, dict):
                            label = str(permission.get("label") or "").strip()
                            if label:
                                labels.append(label)
                        elif isinstance(permission, str):
                            label = permission.strip()
                            if label:
                                labels.append(label)
                    continue

                debug = opportunity.get("_debug") or {}
                detector_id = str(
                    debug.get("detector_id")
                    or opportunity.get("detector_id")
                    or ""
                ).strip()
                labels.extend(get_required_permissions_for_detector(detector_id))

            normalized_stages.append(
                {
                    **stage,
                    "opportunities": with_display_titles(stage.get("opportunities") or []),
                    "requiredPermissions": uniq_permissions_merge(labels),
                }
            )

        display_roadmap["stages"] = normalized_stages
        all_perms = uniq_permissions_merge(
            [
                permission.get("label", "")
                for stage in normalized_stages
                if isinstance(stage, dict)
                for permission in stage.get("requiredPermissions") or []
                if isinstance(permission, dict)
            ]
        )
        display_roadmap["permissionsRequiredCount"] = sum(
            1 for permission in all_perms if permission.get("required")
        )
        display_roadmap["overallReadiness"] = overall_readiness(all_perms)
    return display_roadmap


def with_exec_report_display_titles(report: Dict[str, Any]) -> Dict[str, Any]:
    display_report = dict(report)
    confidence = display_report.get("confidence")
    if isinstance(confidence, str):
        canonical_confidence = {
            "high": "High",
            "moderate": "Moderate",
            "low": "Low",
        }.get(confidence.strip().lower())
        if canonical_confidence:
            display_report["confidence"] = canonical_confidence
    for field in ("topQuickWins", "snapshotBubbles"):
        items = display_report.get(field)
        if isinstance(items, list):
            display_report[field] = with_display_titles(items)
    return display_report
