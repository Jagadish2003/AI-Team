"""
SF-2.7 — Evidence Builder

Converts a DetectorResult + scored opportunity into structured EvidenceObject(s).
Schema defined in SF-1.5 v1.1. This is a pure function.

Key rules from SF-1.5:
  - decision is ALWAYS "UNREVIEWED" — never set by algorithm
  - tsLabel format: datetime.utcnow().strftime("%d %b %Y, %H:%M") — always UTC
  - id prefix: ev_sf_ | ev_sn_ | ev_jira_  (never ev_salesforce_ etc.)
  - snippet MUST contain at least one digit (R1)
  - id_factory optional param for deterministic test IDs
  - Permissive failure: ValueError → return [], caller downgrades confidence to LOW
"""
from __future__ import annotations

import re
import secrets
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from .models import DetectorResult


# ─────────────────────────────────────────────────────────────────────────────
# ID prefix mapping  (SF-1.5 Section 3 — canonical)
# ─────────────────────────────────────────────────────────────────────────────

_SOURCE_PREFIX: Dict[str, str] = {
    "salesforce":  "sf",
    "servicenow":  "sn",
    "jira":        "jira",
}

_ALLOWED_PREFIXES = {"ev_sf_", "ev_sn_", "ev_jira_"}


# ─────────────────────────────────────────────────────────────────────────────
# EvidenceObject dataclass (plain dict in v1 — typed for clarity)
# ─────────────────────────────────────────────────────────────────────────────

def _make_evidence(
    *,
    id: str,
    ts_label: str,
    source: str,
    evidence_type: str,
    title: str,
    snippet: str,
    entities: List[str],
    confidence: str,
) -> Dict[str, Any]:
    """Construct and validate an evidence object per SF-1.5 schema."""
    _validate_evidence(id, ts_label, source, evidence_type, title, snippet, confidence)
    return {
        "id":           id,
        "tsLabel":      ts_label,
        "source":       source,
        "evidenceType": evidence_type,
        "title":        title,
        "snippet":      snippet,
        "entities":     entities,
        "confidence":   confidence,
        "decision":     "UNREVIEWED",   # SF-1.5: ALWAYS UNREVIEWED from algorithm
    }


# ─────────────────────────────────────────────────────────────────────────────
# Validation (SF-1.5 Rules R1–R7)
# ─────────────────────────────────────────────────────────────────────────────

_VALID_SOURCES      = {"Salesforce", "ServiceNow", "Jira"}
_VALID_TYPES        = {"Metric", "Log", "Document", "Survey"}
_VALID_CONFIDENCE   = {"HIGH", "MEDIUM", "LOW"}
_GENERIC_TITLES     = {"high volume detected", "signal detected", "pattern found", "issue detected"}


def _validate_evidence(
    ev_id: str, ts_label: str, source: str, ev_type: str,
    title: str, snippet: str, confidence: str,
) -> None:
    """Raise ValueError if any SF-1.5 rule R1–R7 is violated."""
    # R1: snippet must contain at least one digit
    if not re.search(r"\d", snippet):
        raise ValueError("snippet must contain at least one measurable number")

    # R2: source must be valid enum
    if source not in _VALID_SOURCES:
        raise ValueError(f"source '{source}' is not a valid source system")

    # R3: evidenceType must be valid enum
    if ev_type not in _VALID_TYPES:
        raise ValueError(f"evidenceType '{ev_type}' is not valid")

    # R4: confidence must be valid enum
    if confidence not in _VALID_CONFIDENCE:
        raise ValueError(f"confidence '{confidence}' is not valid")

    # R5: title must be specific, not generic
    if not title or title.strip().lower() in _GENERIC_TITLES:
        raise ValueError("title must be a specific observation, not a generic placeholder")

    # R6: id must match ev_{sf|sn|jira}_{6chars} format
    if not any(ev_id.startswith(p) for p in _ALLOWED_PREFIXES):
        raise ValueError(
            f"id '{ev_id}' does not match required format ev_{{sf|sn|jira}}_{{6chars}}"
        )

    # R7: decision is not validated here — always set to UNREVIEWED by _make_evidence


# ─────────────────────────────────────────────────────────────────────────────
# ID generation
# ─────────────────────────────────────────────────────────────────────────────

def _make_id(signal_source: str, id_factory: Optional[Callable[[], str]] = None) -> str:
    """
    Generate evidence ID per SF-1.5 format: ev_{prefix}_{6chars}.
    id_factory: optional callable returning a 6-char string for deterministic test IDs.
    """
    prefix = _SOURCE_PREFIX.get(signal_source.lower(), "sf")
    suffix = id_factory() if id_factory else secrets.token_hex(3)
    return f"ev_{prefix}_{suffix}"


def _now_utc_label() -> str:
    """SF-1.5 locked format: %d %b %Y, %H:%M UTC. Never local time."""
    return datetime.now(timezone.utc).strftime("%d %b %Y, %H:%M")


# ─────────────────────────────────────────────────────────────────────────────
# Per-detector evidence builders
# ─────────────────────────────────────────────────────────────────────────────

def _build_d1(
    dr: DetectorResult, confidence: str, ts: str, id_factory: Optional[Callable]
) -> Dict[str, Any]:
    ev = dr.raw_evidence
    score = ev.get("flow_activity_score", 0)
    records = ev.get("records_90d", 0)
    count = ev.get("active_flow_count_on_object", 0)
    avg_el = ev.get("element_count", 0)
    label = ev.get("flow_label", "unknown")
    obj = ev.get("trigger_object", "Case")

    snippet = (
        f"{count} active AutoLaunchedFlow(s) on the {obj} object. "
        f"Avg element count: {avg_el} (LOW complexity). "
        f"{records} {obj} records processed in last 90 days "
        f"({round(records/90,1)}/day). "
        f"Flow activity score: {score} (threshold: {dr.threshold})."
    )
    return _make_evidence(
        id=_make_id(dr.signal_source, id_factory),
        ts_label=ts,
        source="Salesforce",
        evidence_type="Metric",   # flow count + activity score are computed metrics
        title=f"Multiple low-complexity AutoLaunchedFlows running on high-volume {obj} object",
        snippet=snippet,
        entities=[f"ent_flow_{label.lower().replace(' ','-').replace('_','-')}"],
        confidence=confidence,
    )


def _build_d2(
    dr: DetectorResult, confidence: str, ts: str, id_factory: Optional[Callable]
) -> Dict[str, Any]:
    ev = dr.raw_evidence
    changes = ev.get("owner_changes_90d", 0)
    total = ev.get("total_cases_90d", 0)
    score = ev.get("handoff_score", 0)
    cats = ev.get("top_categories") or []

    cat_detail = ""
    if cats:
        top = cats[:3]
        cat_detail = " Top categories: " + ", ".join(
            f"{c.get('category','?')} (avg {c.get('handoff_score',0):.1f}/case)" for c in top
        ) + "."

    snippet = (
        f"{changes} owner changes recorded across {total} Cases in the last 90 days. "
        f"Avg {score} owner changes per Case (threshold: {dr.threshold}).{cat_detail}"
    )
    return _make_evidence(
        id=_make_id(dr.signal_source, id_factory),
        ts_label=ts,
        source="Salesforce",
        evidence_type="Metric",
        title="Elevated case owner reassignment rate detected",
        snippet=snippet,
        entities=["ent_case_all_categories", "ent_casehistory_owner_changes"],
        confidence=confidence,
    )


def _build_d3(
    dr: DetectorResult, confidence: str, ts: str, id_factory: Optional[Callable]
) -> Dict[str, Any]:
    ev = dr.raw_evidence
    process = ev.get("process_name", "Approval Process")
    pending = ev.get("pending_count", 0)
    delay = ev.get("avg_delay_days", 0)
    approvers = ev.get("approver_count", 0)
    b_score = ev.get("bottleneck_score", 0)
    proc_slug = process.lower().replace(" ", "_")

    snippet = (
        f"{pending} pending '{process}' records with average delay of "
        f"{delay} days (threshold: {dr.threshold} days). "
        f"Bottleneck score: {b_score} (threshold: 10). "
        f"{approvers} approver(s) handling all pending items."
    )
    return _make_evidence(
        id=_make_id(dr.signal_source, id_factory),
        ts_label=ts,
        source="Salesforce",
        evidence_type="Metric",
        title=f"Approval records exceeding SLA due to high delay in '{process}'",
        snippet=snippet,
        entities=[f"ent_approval_{proc_slug}"],
        confidence=confidence,
    )


def _build_d4(
    dr: DetectorResult, confidence: str, ts: str, id_factory: Optional[Callable]
) -> Dict[str, Any]:
    ev = dr.raw_evidence
    closed = ev.get("closed_cases_90d", 0)
    linked = ev.get("cases_with_kb_link", 0)
    score = ev.get("knowledge_gap_score", 0)
    pct = round((1 - score) * 100, 1) if score <= 1 else 0.0

    snippet = (
        f"{linked} of {closed} closed Cases in the last 90 days have a linked "
        f"Knowledge Article. Knowledge gap score: {score} (threshold: {dr.threshold}). "
        f"{pct}% of Cases closed with KB reuse — "
        f"{round((1-pct/100)*closed)} resolved without KB linkage."
    )
    return _make_evidence(
        id=_make_id(dr.signal_source, id_factory),
        ts_label=ts,
        source="Salesforce",
        evidence_type="Metric",
        title="Significant knowledge gap in Case resolution — low KB article linkage",
        snippet=snippet,
        entities=["ent_case_closed", "ent_knowledge_articles"],
        confidence=confidence,
    )


def _build_d5(
    dr: DetectorResult, confidence: str, ts: str, id_factory: Optional[Callable]
) -> Dict[str, Any]:
    ev = dr.raw_evidence
    cred = ev.get("credential_name", "Named Credential")
    dev_name = ev.get("credential_developer_name", "")
    ref_count = ev.get("flow_reference_count", 0)
    flow_ids = ev.get("referencing_flow_ids") or []
    match_type = ev.get("match_type", "name")
    cred_slug = dev_name.lower().replace(" ", "_") if dev_name else cred.lower().replace(" ", "_")

    snippet = (
        f"{ref_count} active Flow(s) reference the '{cred}' Named Credential "
        f"(threshold: {int(dr.threshold)} distinct flows). "
        f"Match type: {match_type}. "
        f"Referencing flow IDs: {', '.join(str(f) for f in flow_ids[:5])}."
    )
    return _make_evidence(
        id=_make_id(dr.signal_source, id_factory),
        ts_label=ts,
        source="Salesforce",
        evidence_type="Metric",   # flow reference count is a computed metric
        title=f"Multiple Flows referencing the same Named Credential '{cred}'",
        snippet=snippet,
        entities=[f"ent_credential_{cred_slug}"],
        confidence=confidence,
    )


def _build_d6(
    dr: DetectorResult, confidence: str, ts: str, id_factory: Optional[Callable]
) -> Dict[str, Any]:
    ev = dr.raw_evidence
    process = ev.get("process_name", "Approval Process")
    pending = ev.get("pending_count", 0)
    approvers = ev.get("approver_count", 0)
    b_score = ev.get("bottleneck_score", 0)
    proc_slug = process.lower().replace(" ", "_")

    approver_note = ev.get("approver_type_notes", "")
    if "Role/Queue" in approver_note:
        approver_note_str = " Note: approver count may undercount capacity (Role/Queue actors present)."
    else:
        approver_note_str = ""

    snippet = (
        f"{pending} pending '{process}' records. "
        f"Only {approvers} active approver(s) identified via ProcessInstanceWorkitem. "
        f"Bottleneck score: {b_score} pending per approver (threshold: {dr.threshold}).{approver_note_str}"
    )
    return _make_evidence(
        id=_make_id(dr.signal_source, id_factory),
        ts_label=ts,
        source="Salesforce",
        evidence_type="Metric",
        title=f"Approval queue overloaded with limited approver capacity in '{process}'",
        snippet=snippet,
        entities=[f"ent_approval_{proc_slug}", "ent_user_approvers"],
        confidence=confidence,
    )


def _build_d7(
    dr: DetectorResult, confidence: str, ts: str, id_factory: Optional[Callable]
) -> Dict[str, Any]:
    ev = dr.raw_evidence
    sf_count = ev.get("sf_echo_count", 0)
    sf_total = ev.get("sf_total_cases", 0)
    sf_score = ev.get("sf_echo_score", 0.0)
    sn_count = ev.get("sn_match_count", 0)
    sn_total = ev.get("sn_total_incidents", 0)
    sn_score = ev.get("sn_echo_score", 0.0)
    jira_score = ev.get("jira_echo_score", 0.0)
    patterns = ev.get("matched_patterns") or []

    # Build three-system narrative
    parts = []
    if sf_total > 0:
        parts.append(
            f"{sf_count} of {sf_total} Salesforce Cases reference external ticket IDs "
            f"(SF echo score: {sf_score}, threshold: {dr.threshold})"
        )
    if sn_total > 0:
        parts.append(
            f"{sn_count} of {sn_total} ServiceNow incidents reference Salesforce case IDs "
            f"(SN echo score: {sn_score})"
        )
    if jira_score > 0:
        jira_labels = ev.get("jira_sf_label_count", 0)
        jira_issues = ev.get("jira_total_issues", 0)
        if jira_issues > 0:
            parts.append(
                f"{jira_labels} of {jira_issues} Jira CRM issues reference Salesforce cases "
                f"(Jira echo score: {jira_score})"
            )

    pattern_str = f" Patterns matched: {', '.join(patterns)}." if patterns else ""
    snippet = ". ".join(parts) + "." + pattern_str

    # Determine signal_source display name
    source_map = {"salesforce": "Salesforce", "servicenow": "ServiceNow", "jira": "Jira"}
    source = source_map.get(dr.signal_source, "Salesforce")

    return _make_evidence(
        id=_make_id(dr.signal_source, id_factory),
        ts_label=ts,
        source=source,
        evidence_type="Log",      # cross-system echo found in individual records/tickets
        title="Cross-system ticket duplication detected across Salesforce and external systems",
        snippet=snippet,
        entities=["ent_case_all_categories", "ent_incident_servicenow_access"],
        confidence=confidence,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Dispatch table
# ─────────────────────────────────────────────────────────────────────────────

_BUILDERS = {
    "REPETITIVE_AUTOMATION":    _build_d1,
    "HANDOFF_FRICTION":         _build_d2,
    "APPROVAL_BOTTLENECK":      _build_d3,
    "KNOWLEDGE_GAP":            _build_d4,
    "INTEGRATION_CONCENTRATION":_build_d5,
    "PERMISSION_BOTTLENECK":    _build_d6,
    "CROSS_SYSTEM_ECHO":        _build_d7,
}


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def build_evidence(
    detector_result: DetectorResult,
    opportunity: Dict[str, Any],
    id_factory: Optional[Callable[[], str]] = None,
) -> List[Dict[str, Any]]:
    """
    Convert raw_evidence from DetectorResult into one or more EvidenceObject dicts.

    Parameters
    ----------
    detector_result : DetectorResult
        Output of a fired detector (SF-2.5).
    opportunity : dict
        Scored opportunity dict from scorer.score() (SF-2.6).
        Must contain 'confidence' key.
    id_factory : optional callable() -> str
        For deterministic test IDs. Pass None in production (uses secrets.token_hex(3)).
        Example: counter = itertools.count(1); factory = lambda: f"{next(counter):06x}"

    Returns
    -------
    List[Dict]
        List of evidence objects (usually 1). Empty list if construction fails.
        Caller must downgrade opportunity confidence to LOW if list is empty.

    Notes
    -----
    - decision is ALWAYS "UNREVIEWED" — SF-2.x never sets APPROVED/REJECTED.
    - tsLabel is always UTC in format "%d %b %Y, %H:%M".
    - id prefix: ev_sf_ / ev_sn_ / ev_jira_ only.
    - Raises nothing — failures logged and empty list returned (permissive mode).
    """
    confidence = str(opportunity.get("confidence", "LOW"))
    ts = _now_utc_label()

    builder = _BUILDERS.get(detector_result.detector_id)
    if builder is None:
        # Unknown detector — return empty (caller downgrades confidence)
        return []

    try:
        evidence = builder(detector_result, confidence, ts, id_factory)
        return [evidence]
    except ValueError:
        # R1–R7 violation — permissive failure per SF-1.5
        return []
    except Exception:
        # Unexpected failure — permissive failure
        return []
