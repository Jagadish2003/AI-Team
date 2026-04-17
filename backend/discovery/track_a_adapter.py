"""
SF-2.8 — Track A Adapter

Converts the Track B internal runner payload (detector-centric) into
the exact OpportunityCandidate shape that Track A's seed_loader and
TypeScript contract expect.

Track A TypeScript OpportunityCandidate shape (from A-Task-1 contract v1.2):
    id:                   str   — stable opp_NNN identifier
    title:                str   — human-readable opportunity title
    category:             str   — category label for S6/S7 grouping
    tier:                 str   — Quick Win | Strategic | Complex
    decision:             str   — UNREVIEWED (always from algorithm)
    impact:               int   — 1-10
    effort:               int   — 1-10
    confidence:           str   — HIGH | MEDIUM | LOW
    aiRationale:          str   — AI-generated rationale paragraph
    evidenceIds:          list  — list of evidence object IDs
    requiredPermissions:  list  — permission strings
    override:             dict  — {isLocked, rationaleOverride, overrideReason, updatedAt}

Track A EvidenceReview shape (from A-Task-1 contract v1.2):
    id, tsLabel, source, evidenceType, title, snippet,
    entities, confidence, decision
    (All fields already produced correctly by SF-2.7 evidence_builder)
"""
from __future__ import annotations

import itertools
from typing import Any, Dict, List, Optional

# ─────────────────────────────────────────────────────────────────────────────
# Title + category mapping per detector
# ─────────────────────────────────────────────────────────────────────────────

_DETECTOR_META: Dict[str, Dict[str, str]] = {
    "REPETITIVE_AUTOMATION": {
        "title_template":   "Automate repetitive {trigger_object} processing flows",
        "category":         "Automation Opportunity",
        "rationale_template": (
            "{count} active low-complexity AutoLaunchedFlow(s) are processing "
            "{records_90d} {trigger_object} records in 90 days (activity score "
            "{score:.2f}, threshold {threshold}). These flows perform high-volume "
            "repetitive logic with fewer than {element_count} elements each — "
            "a strong candidate for an Agentforce agent that handles decisioning "
            "and exception routing rather than a static flow."
        ),
        "required_permissions": [
            "Salesforce: read FlowVersionView (Tooling API)",
            "Salesforce: read Flow Metadata (Tooling API)",
        ],
    },
    "HANDOFF_FRICTION": {
        "title_template":   "Reduce case routing friction with intelligent assignment",
        "category":         "Ticket Routing",
        "rationale_template": (
            "{owner_changes} owner changes recorded across {total_cases} Cases in "
            "90 days — an average of {score:.1f} reassignments per case (threshold "
            "{threshold}). Agents are manually re-routing cases that pattern analysis "
            "could route correctly on first assignment. An intelligent routing agent "
            "would reduce time-to-resolution and eliminate repetitive escalation cycles."
        ),
        "required_permissions": [
            "Salesforce: read CaseHistory",
            "Salesforce: read Case",
        ],
    },
    "APPROVAL_BOTTLENECK": {
        "title_template":   "Streamline {process_name} approval bottleneck",
        "category":         "Approval Automation",
        "rationale_template": (
            "{pending} records are pending in the '{process_name}' approval process "
            "with an average delay of {delay:.1f} days (threshold {threshold} days). "
            "{approvers} approver(s) are handling all pending items (bottleneck score "
            "{b_score:.1f}). An automation agent could pre-validate submissions, "
            "surface approval context, and escalate stale approvals automatically."
        ),
        "required_permissions": [
            "Salesforce: read ProcessInstance",
            "Salesforce: read ProcessInstanceWorkitem",
            "Salesforce: read ProcessDefinition",
        ],
    },
    "KNOWLEDGE_GAP": {
        "title_template":   "Automate knowledge article surfacing at case resolution",
        "category":         "Knowledge Management",
        "rationale_template": (
            "{closed} Cases closed in 90 days — only {linked} had a linked Knowledge "
            "Article (knowledge gap score {score:.2f}, threshold {threshold}). "
            "Resolution agents are not consistently reusing KB content, increasing "
            "handle time and reducing deflection. An Agentforce agent could surface "
            "relevant KB articles during resolution and auto-link them on close."
        ),
        "required_permissions": [
            "Salesforce: read Case",
            "Salesforce: read CaseArticle",
        ],
    },
    "INTEGRATION_CONCENTRATION": {
        "title_template":   "Consolidate flows referencing '{credential_name}'",
        "category":         "Integration Governance",
        "rationale_template": (
            "{ref_count} active Flow(s) independently call the '{credential_name}' "
            "Named Credential (threshold {threshold:.0f} distinct flows). These flows "
            "contain duplicated integration callout logic with no shared error handling "
            "or governance layer. An agent-based orchestration pattern would centralise "
            "the integration, add retry/fallback handling, and reduce maintenance risk."
        ),
        "required_permissions": [
            "Salesforce: read NamedCredential (Tooling API)",
            "Salesforce: read Flow Metadata (Tooling API)",
        ],
    },
    "PERMISSION_BOTTLENECK": {
        "title_template":   "Redistribute approval workload for '{process_name}'",
        "category":         "Approval Automation",
        "rationale_template": (
            "{pending} records are queued in '{process_name}' with only "
            "{approvers} approver(s) — a bottleneck score of {b_score:.1f} pending "
            "items per approver (threshold {threshold}). This approval concentration "
            "creates a single point of failure. An automation layer could distribute "
            "assignments intelligently or escalate to backup approvers when the queue "
            "exceeds acceptable depth."
        ),
        "required_permissions": [
            "Salesforce: read ProcessInstance",
            "Salesforce: read ProcessInstanceWorkitem",
        ],
    },
    "CROSS_SYSTEM_ECHO": {
        "title_template":   "Automate cross-system record sync (Salesforce ↔ external)",
        "category":         "Sync Automation",
        "rationale_template": (
            "Cross-system echo detected: {sf_count} Salesforce Cases reference external "
            "ticket IDs (SF echo score {sf_score:.2f}){sn_part}{jira_part}. "
            "Agents are manually duplicating the same issue across systems — a pattern "
            "that agent-based bidirectional sync would eliminate, reducing manual effort "
            "and improving resolution continuity across teams."
        ),
        "required_permissions": [
            "Salesforce: read Case",
            "ServiceNow: read incident (if applicable)",
            "Jira: read issues (if applicable)",
        ],
    },
}


def _override_default() -> Dict[str, Any]:
    """Default override object — Track A expects this shape on every opportunity."""
    return {
        "isLocked":          False,
        "rationaleOverride": "",
        "overrideReason":    "",
        "updatedAt":         None,
    }


def _format_title(detector_id: str, ev: Dict[str, Any]) -> str:
    """Produce a human-readable title by filling the template with raw_evidence."""
    meta = _DETECTOR_META.get(detector_id, {})
    tmpl = meta.get("title_template", detector_id.replace("_", " ").title())
    try:
        return tmpl.format(
            trigger_object=ev.get("trigger_object", "record"),
            process_name=ev.get("process_name", "Approval"),
            credential_name=ev.get("credential_name", "integration"),
        )
    except (KeyError, ValueError):
        return tmpl


def _format_rationale(
    detector_id: str,
    ev: Dict[str, Any],
    metric_value: float,
    threshold: float,
) -> str:
    """Generate the aiRationale paragraph from raw_evidence values."""
    meta = _DETECTOR_META.get(detector_id, {})
    tmpl = meta.get("rationale_template", "Pattern detected by AgentIQ discovery algorithm.")

    # Build D7 SN/Jira parts
    sn_score = float(ev.get("sn_echo_score", 0.0))
    jira_score = float(ev.get("jira_echo_score", 0.0))
    sn_part = (
        f", {ev.get('sn_match_count', 0)} ServiceNow incidents reference "
        f"SF cases (SN score {sn_score:.2f})"
        if sn_score > 0 else ""
    )
    jira_part = (
        f", {ev.get('jira_sf_label_count', 0)} Jira issues reference SF cases "
        f"(Jira score {jira_score:.2f})"
        if jira_score > 0 else ""
    )

    try:
        return tmpl.format(
            # Common
            score=metric_value,
            threshold=threshold,
            # D1
            count=ev.get("active_flow_count_on_object", 0),
            trigger_object=ev.get("trigger_object", "record"),
            records_90d=ev.get("records_90d", 0),
            element_count=ev.get("element_count", 0),
            # D2
            owner_changes=ev.get("owner_changes_90d", 0),
            total_cases=ev.get("total_cases_90d", 0),
            # D3/D6
            pending=ev.get("pending_count", 0),
            process_name=ev.get("process_name", "Approval"),
            delay=float(ev.get("avg_delay_days", 0)),
            approvers=ev.get("approver_count", 0),
            b_score=float(ev.get("bottleneck_score", 0)),
            # D4
            closed=ev.get("closed_cases_90d", 0),
            linked=ev.get("cases_with_kb_link", 0),
            # D5
            ref_count=ev.get("flow_reference_count", 0),
            credential_name=ev.get("credential_name", "integration"),
            # D7
            sf_count=ev.get("sf_echo_count", 0),
            sf_score=float(ev.get("sf_echo_score", 0)),
            sn_part=sn_part,
            jira_part=jira_part,
        )
    except (KeyError, ValueError):
        return tmpl


# ─────────────────────────────────────────────────────────────────────────────
# Public adapter
# ─────────────────────────────────────────────────────────────────────────────

def to_track_a_opportunities(
    runner_payload: Dict[str, Any],
    id_counter: Optional[itertools.count] = None,
) -> List[Dict[str, Any]]:
    """
    Convert Track B runner payload to Track A OpportunityCandidate[] shape.

    Parameters
    ----------
    runner_payload : dict
        Full payload from runner.run() including 'opportunities' list.
    id_counter : optional itertools.count
        For deterministic IDs in testing. Defaults to sequential from 1.

    Returns
    -------
    List[dict]
        List ready for Track A seed_loader.py — exactly matches OpportunityCandidate TS type.
    """
    if id_counter is None:
        id_counter = itertools.count(1)

    opportunities = runner_payload.get("opportunities", [])
    result: List[Dict[str, Any]] = []

    for opp in opportunities:
        did = opp.get("detector_id", "")
        ev = opp.get("raw_evidence") or {}
        metric_value = float(opp.get("metric_value", 0))
        threshold = float(opp.get("threshold", 0))

        meta = _DETECTOR_META.get(did, {})
        opp_id = f"opp_{next(id_counter):03d}"

        track_a_opp: Dict[str, Any] = {
            # Required Track A fields
            "id":          opp_id,
            "title":       _format_title(did, ev),
            "category":    meta.get("category", "Automation Opportunity"),
            "tier":        opp.get("tier", "Strategic"),
            "decision":    "UNREVIEWED",
            "impact":      int(opp.get("impact", 1)),
            "effort":      int(opp.get("effort", 1)),
            "confidence":  opp.get("confidence", "LOW"),
            "aiRationale": _format_rationale(did, ev, metric_value, threshold),
            "evidenceIds": opp.get("evidenceIds", []),
            "requiredPermissions": meta.get("required_permissions", []),
            "override":    _override_default(),
            # Keep calibration fields under a debug namespace (not breaking Track A)
            "_debug": {
                "detector_id":   did,
                "signal_source": opp.get("signal_source", ""),
                "metric_value":  metric_value,
                "threshold":     threshold,
                "roadmap_stage": opp.get("roadmap_stage", ""),
                "score_debug":   opp.get("score_debug", {}),
            },
        }
        result.append(track_a_opp)

    return result


def to_track_a_evidence(
    runner_payload: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """
    Extract all evidence objects from runner payload.
    Evidence shape is already Track A compatible (produced by SF-2.7 evidence_builder).
    Returns flat list suitable for Track A evidence store.
    """
    evidence: List[Dict[str, Any]] = []
    seen_ids = set()
    for opp in runner_payload.get("opportunities", []):
        for ev in opp.get("evidence", []):
            eid = ev.get("id")
            if eid and eid not in seen_ids:
                evidence.append(ev)
                seen_ids.add(eid)
    return evidence


def export_track_a_seed(
    runner_payload: Dict[str, Any],
    id_counter: Optional[itertools.count] = None,
) -> Dict[str, Any]:
    """
    Produce the full seed payload for Track A seed_loader.py.

    Returns dict with:
        opportunities: List[OpportunityCandidate]   — Track A shape
        evidence:      List[EvidenceReview]          — already correct shape
        run_meta:      dict                          — run metadata
    """
    opps = to_track_a_opportunities(runner_payload, id_counter)
    evs = to_track_a_evidence(runner_payload)

    return {
        "opportunities": opps,
        "evidence":      evs,
        "run_meta": {
            "runId":       runner_payload.get("runId"),
            "orgId":       runner_payload.get("orgId"),
            "mode":        runner_payload.get("mode"),
            "startedAt":   runner_payload.get("startedAt"),
            "completedAt": runner_payload.get("completedAt"),
            "inputs":      runner_payload.get("inputs", {}),
        },
    }
