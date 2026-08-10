"""
backend/app/corroboration_engine.py

ENT-2 — Cross-System Confidence Elevation — Corroboration Engine (T1).

This is the central, shared engine that decides whether a finding is supported
by other connected systems. Before ENT-2, confidence elevation was handled
inconsistently inside individual scorers (lending_scorer, strs_benefits_scorer,
github_engineering_scorer). This engine centralises that logic so corroboration
is consistent, explicit, testable, and visible on the opportunity card.

The engine is NOT pack-specific. It evaluates corroboration for any detector
against any connected-system data, so ENT-5 (ServiceNow + Jira pack) and future
stories reuse it unchanged.

Rule definitions live in backend/discovery/packs/corroboration_rules.py (data).
This module holds only the BEHAVIOUR — the eight rule check functions and the
orchestration in evaluate_corroboration(). Keeping rules-as-data separate from
engine-as-logic means a reviewer can audit which rules exist without reading
this file.

────────────────────────────────────────────────────────────────────────────
run_data CONTRACT (what the engine reads)
────────────────────────────────────────────────────────────────────────────
The engine reads a single ``run_data`` dict aggregating all connected-system
signals for the run. Every access is defensive (.get with defaults) so a
missing or partially-populated system never raises — a missing system simply
does not corroborate.

    run_data = {
        # Names of systems connected for this run. Drives COR-08 (single source).
        "connected_systems": ["salesforce", "servicenow", "jira", ...],

        "servicenow": {
            "incidents": [
                {
                    # one of: created | sys_created_on | opened | created_at | timestamp
                    "sys_created_on": "2026-06-01T10:00:00Z",
                    "state": "Open",                 # open unless Closed/Resolved/Cancelled
                    "team": "lending-ops",           # optional assignment group / team
                    "detector_ids": ["COVENANT_TRACKING_GAP"],  # optional linkage
                },
                ...
            ],
        },

        "jira": {
            "issues": [
                {
                    "created": "2026-06-01T10:00:00Z",
                    "status": "Open",                # unresolved unless Done/Closed/Resolved
                    "process": "covenant",           # optional process linkage
                    "detector_ids": ["COVENANT_TRACKING_GAP"],  # optional linkage
                },
                ...
            ],
            "sprint_velocity": {                     # COR-07
                "declined": True,
                "team": "lending-eng",
                "timestamp": "2026-06-01T10:00:00Z",
            },
        },

        "confluence": {                              # COR-04
            "covenant_documentation_present": False,
        },

        "slack": {                                   # COR-05 / COR-06
            "escalation_pattern": {
                "fired": True,
                "timestamp": "2026-06-01T10:00:00Z",
            },
        },

        "java_app": {                                # COR-09 (R17-A3)
            "operational_friction": {
                "fired": True,
                "timestamp": "2026-06-01T10:00:00Z",
                "services": ["payments"],
                "reasons": ["elevated error rate", "latency degradation"],
            },
        },

        "dotnet_app": {                              # COR-10 (R17-A4)
            "operational_friction": {                # same shape as java_app —
                "fired": True,                       # observed evidence, elevates
                "timestamp": "2026-06-01T10:00:00Z",
                "services": ["orders"],
                "reasons": ["elevated error rate", "latency degradation"],
            },
        },
    }

────────────────────────────────────────────────────────────────────────────
TIME WINDOW
────────────────────────────────────────────────────────────────────────────
CORROBORATION_WINDOW_DAYS = 30. Every time-sensitive check requires the
supporting signal to have occurred within 30 days of the run timestamp. A
ServiceNow incident from 31+ days ago does NOT corroborate (ENT-2 AC6).

────────────────────────────────────────────────────────────────────────────
THE SLACK CEILING (hardcoded principle)
────────────────────────────────────────────────────────────────────────────
COR-05 is the only rule that records a supporting source WITHOUT elevating
confidence. Slack escalation alone keeps confidence at MEDIUM. Slack elevates
to HIGH only when combined with a primary corroborator (COR-06). Do not add a
rule that elevates Slack-only to HIGH.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

try:
    from backend.discovery.packs.corroboration_rules import (
        CORROBORATION_WINDOW_DAYS,
        CONFIDENCE_HIGH,
        CONFIDENCE_MEDIUM,
        CONFIDENCE_ORDER,
        RULE_CARD_LABELS,
        SINGLE_SOURCE_LABEL,
        TRIPLE_CORROBORATION_LABEL,
    )
except ModuleNotFoundError:  # Runtime inside backend/ where discovery is top-level.
    from discovery.packs.corroboration_rules import (
        CORROBORATION_WINDOW_DAYS,
        CONFIDENCE_HIGH,
        CONFIDENCE_MEDIUM,
        CONFIDENCE_ORDER,
        RULE_CARD_LABELS,
        SINGLE_SOURCE_LABEL,
        TRIPLE_CORROBORATION_LABEL,
    )

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

#: ServiceNow/Jira states that mean a record is NOT open/unresolved.
_CLOSED_STATES = {"closed", "resolved", "cancelled", "canceled", "done", "complete", "completed"}

#: Detector that drives the Confluence documentation-gap rule (COR-04).
_COVENANT_DETECTOR = "COVENANT_TRACKING_GAP"

#: Detector that drives the Jira sprint-velocity rule (COR-07). Several spellings
#: appear across packs, so match defensively.
_LOAN_ORIGINATION_DETECTORS = {
    "LOAN_ORIGINATION_BOTTLENECK",
    "LOAN_ORIGINATION_ROUTING_FRICTION",
}

#: Timestamp field names accepted on incident / issue records.
_TIMESTAMP_KEYS = (
    "created", "sys_created_on", "opened", "created_at",
    "timestamp", "opened_at", "createdAt", "openedAt",
)

#: R16-C1 C1: a single corroborating source must reach this weighted authority
#: before it may lead a MEDIUM -> HIGH corroboration elevation. The value is the
#: workflow_system secondary weight, so system-of-record and workflow/process
#: authorities can lead, while supporting/documentation sources cannot lead by
#: priority alone.
_CORROBORATION_LEAD_WEIGHT_MIN = 0.80

#: Mapping from fired corroboration rules to the system whose configured
#: role/priority should modulate that corroboration contribution.
_CORROBORATION_RULE_SYSTEMS: Dict[str, str] = {
    "COR-01": "servicenow",
    "COR-02": "jira",
    "COR-04": "confluence",
    "COR-07": "jira",
    # R17-A3 §3: Java-app operational friction is first-class OBSERVED evidence
    # (directly measured), so COR-09 is an ELEVATING corroborator like COR-01/02 —
    # not a supporting-only ceiling like Slack (COR-05).
    "COR-09": "java_app",
    # R17-A4 §3: the .NET counterpart to COR-09 — same observed-evidence basis, so
    # COR-10 elevates identically. Not a separate confidence model; the .NET signal
    # plugs into this same cross-system corroboration mapping.
    "COR-10": "dotnet_app",
}


# ─────────────────────────────────────────────────────────────────────────────
# Result dataclass
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class CorroborationResult:
    """The single, predictable output shape of the corroboration engine.

    Returned by evaluate_corroboration() in ALL cases — including when no
    corroboration occurred — so the scoring pipeline can rely on a consistent
    structure.

    Fields
    ------
    rule_ids:
        Every rule that fired, e.g. ['COR-01', 'COR-02']. Surfaced on
        OppEnrichment.corroboration_rule_ids for auditability (AC9).
    corroboration_sources:
        Human-readable source labels, e.g. ['ServiceNow', 'Jira']. Empty when
        there is no corroboration (single source / no support) so the UI badge
        does not render (AC7).
    corroboration_label:
        The display string for the opportunity card.
    elevated_confidence:
        The confidence the corroboration rules support: 'LOW' | 'MEDIUM' | 'HIGH'.
        This is the corroboration verdict, not necessarily the final applied
        confidence (the pipeline never downgrades the scorer's confidence).
    original_confidence:
        The pre-corroboration baseline assumed by the engine (MEDIUM per spec).
    triple_corroboration:
        True when COR-03 applies (Salesforce + ServiceNow + Jira).
    confidence_elevated:
        Convenience flag — True when elevated_confidence is higher than
        original_confidence.
    elevation_target:
        The confidence level the fired elevating rules target ('HIGH' when any
        elevating rule fired, else 'MEDIUM').
    corroboration_weight_debug:
        R16-C1 audit details showing how Stack Builder source role/priority
        shaped the corroboration verdict.
    identity_gate:
        2.0-B2 T6 (AC5) audit details for the cross-source identity gate: whether
        the two sources' entity references were claimed to be the same thing,
        whether that identity was genuinely RESOLVED (and by which rule), and
        which rules were consequently refused an elevation. Empty when the gate
        did not engage. ``identity_verified`` inside it is how a reviewer tells a
        HIGH resting on a proven shared identity from one resting only on a
        shared detector.
    """

    rule_ids: List[str] = field(default_factory=list)
    corroboration_sources: List[str] = field(default_factory=list)
    corroboration_label: Optional[str] = None
    elevated_confidence: str = CONFIDENCE_MEDIUM
    original_confidence: str = CONFIDENCE_MEDIUM
    triple_corroboration: bool = False
    confidence_elevated: bool = False
    elevation_target: str = CONFIDENCE_MEDIUM
    corroboration_weight_debug: Dict[str, Any] = field(default_factory=dict)
    identity_gate: Dict[str, Any] = field(default_factory=dict)


def _empty_result(original_confidence: str = CONFIDENCE_MEDIUM) -> CorroborationResult:
    """A no-corroboration result. Used on single-source runs and on any error."""
    return CorroborationResult(
        rule_ids=[],
        corroboration_sources=[],
        corroboration_label=None,
        elevated_confidence=original_confidence,
        original_confidence=original_confidence,
        triple_corroboration=False,
        confidence_elevated=False,
        elevation_target=original_confidence,
        corroboration_weight_debug={},
    )


# ─────────────────────────────────────────────────────────────────────────────
# Time-window helpers
# ─────────────────────────────────────────────────────────────────────────────

def _parse_timestamp(value: Any) -> Optional[datetime]:
    """Parse a timestamp from common string/`datetime` forms. Returns tz-aware UTC.

    Returns None when the value cannot be parsed — the caller treats an
    unparseable timestamp as NOT within the window (fail-safe: no elevation).
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None
    # Normalise trailing Z to +00:00 for fromisoformat.
    candidate = raw.replace("Z", "+00:00")
    for parser in (
        lambda s: datetime.fromisoformat(s),
        lambda s: datetime.strptime(s, "%Y-%m-%d %H:%M:%S"),
        lambda s: datetime.strptime(s, "%Y-%m-%d"),
    ):
        try:
            dt = parser(candidate)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
    return None


def _record_timestamp(record: Dict[str, Any]) -> Optional[datetime]:
    """Extract the first recognised timestamp from a record dict."""
    for key in _TIMESTAMP_KEYS:
        if key in record:
            dt = _parse_timestamp(record.get(key))
            if dt is not None:
                return dt
    return None


def _within_window(
    record: Dict[str, Any],
    run_timestamp: datetime,
    window_days: int = CORROBORATION_WINDOW_DAYS,
) -> bool:
    """True if the record's timestamp is within window_days before run_timestamp.

    A record with no parseable timestamp is treated as OUTSIDE the window
    (fail-safe: stale or unknown-age data must not elevate confidence).
    """
    ts = _record_timestamp(record)
    if ts is None:
        return False
    if run_timestamp.tzinfo is None:
        run_timestamp = run_timestamp.replace(tzinfo=timezone.utc)
    cutoff = run_timestamp - timedelta(days=window_days)
    # Within window: cutoff <= ts <= run_timestamp. Future-dated records must
    # not corroborate the current run.
    return cutoff <= ts <= run_timestamp


def _is_open(record: Dict[str, Any]) -> bool:
    """True when a ServiceNow incident / Jira issue is open / unresolved.

    Defaults to open when no state/status field is present (a record passed in
    as a corroborator is assumed relevant unless explicitly closed).
    """
    state = (
        record.get("state")
        or record.get("status")
        or record.get("incident_state")
        or ""
    )
    if isinstance(state, dict):  # Jira sometimes nests {"name": "..."}
        state = state.get("name", "")
    return str(state).strip().lower() not in _CLOSED_STATES


def _linked_to_detector(record: Dict[str, Any], detector_id: str) -> bool:
    """True when a record is linked to detector_id.

    Linkage is established by an explicit ``detector_ids`` list on the record.
    When a record carries NO ``detector_ids`` field at all, it is treated as a
    pre-filtered relevant record (the ingestor already scoped it) and matches.
    This keeps both controlled test data (explicit linkage) and real pipeline
    data (pre-filtered correlation sets) working.
    """
    if "detector_ids" not in record:
        return True
    linked = record.get("detector_ids") or []
    if isinstance(linked, str):
        linked = [linked]
    return detector_id in {str(x) for x in linked}


# ─────────────────────────────────────────────────────────────────────────────
# System-data extraction helpers (defensive)
# ─────────────────────────────────────────────────────────────────────────────

def _system_block(run_data: Dict[str, Any], key: str) -> Dict[str, Any]:
    """Return run_data[key] as a dict, or {} when missing or malformed.

    Defensive: a non-dict value (e.g. a stray string) yields {} rather than
    raising — supporting the non-blocking guarantee (AC10).
    """
    block = (run_data or {}).get(key)
    return block if isinstance(block, dict) else {}


def _servicenow_incidents(run_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    incidents = _system_block(run_data, "servicenow").get("incidents")
    if isinstance(incidents, list):
        return [i for i in incidents if isinstance(i, dict)]
    return []


def _jira_issues(run_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    issues = _system_block(run_data, "jira").get("issues")
    if isinstance(issues, list):
        return [i for i in issues if isinstance(i, dict)]
    return []


def _connected_systems(run_data: Dict[str, Any]) -> List[str]:
    systems = (run_data or {}).get("connected_systems") or []
    if isinstance(systems, (list, tuple, set)):
        return [str(s).strip().lower() for s in systems if str(s).strip()]
    return []


def normalise_connected_systems(systems: Any) -> List[str]:
    """Return stable business system ids for corroboration rules."""
    normalised = set()
    for system in systems or []:
        system_id = str(system).strip().lower()
        if not system_id:
            continue
        if system_id in {"salesforce_ncino", "ncino"}:
            normalised.add("salesforce")
        elif system_id == "jira_confluence":
            normalised.update({"jira", "confluence"})
        else:
            normalised.add(system_id)
    return sorted(normalised)


def _dict_or_empty(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _iter_candidate_blocks(payload: Dict[str, Any], *, depth: int = 0) -> List[Dict[str, Any]]:
    """Return likely correlation containers without requiring caller-specific code."""
    if not isinstance(payload, dict) or depth > 3:
        return []
    blocks = [payload]
    for key in (
        "lending_correlation",
        "jira_strs_correlation",
        "sn_strs_correlation",
        "strs_benefits",
        "corroboration",
        "corroboration_data",
        "cross_system_evidence",
        "external_corroboration",
    ):
        child = payload.get(key)
        if isinstance(child, dict):
            blocks.extend(_iter_candidate_blocks(child, depth=depth + 1))
    return blocks


def _find_corroboration_block(system_id: str, payloads: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Find a Slack/Confluence corroboration block in available run payloads."""
    direct_keys = (
        system_id,
        f"{system_id}_corroboration",
        f"{system_id}_evidence",
        f"{system_id}_signals",
    )
    for payload in payloads:
        for block in _iter_candidate_blocks(payload):
            for key in direct_keys:
                found = _dict_or_empty(block.get(key))
                if found:
                    return found
    return {}


def _detector_ids_from_record(record: Dict[str, Any], default_detector_id: Optional[str] = None) -> List[str]:
    linked = (
        record.get("detector_ids")
        or record.get("detector_id")
        or record.get("detectorId")
        or default_detector_id
    )
    if linked is None:
        return []
    if isinstance(linked, str):
        linked = [linked]
    if not isinstance(linked, (list, tuple, set)):
        return []
    return [str(item) for item in linked if str(item).strip()]


def _timestamp_value(record: Dict[str, Any]) -> Any:
    for key in _TIMESTAMP_KEYS:
        if record.get(key) is not None:
            return record.get(key)
    return None


def _add_record_by_detector(
    records_by_detector: Dict[str, List[Dict[str, Any]]],
    record: Dict[str, Any],
    *,
    default_detector_id: Optional[str] = None,
) -> None:
    for detector_id in _detector_ids_from_record(record, default_detector_id):
        enriched = dict(record)
        enriched.setdefault("detector_ids", [detector_id])
        records_by_detector.setdefault(detector_id, []).append(enriched)


def _extract_matched_records(
    payloads: List[Dict[str, Any]],
    record_keys: tuple[str, ...],
) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    for payload in payloads:
        for block in _iter_candidate_blocks(payload):
            for key in record_keys:
                value = block.get(key)
                if isinstance(value, list):
                    records.extend(item for item in value if isinstance(item, dict))
    return records


def _add_detector_map_records(
    records_by_detector: Dict[str, List[Dict[str, Any]]],
    detector_map: Dict[str, List[Any]],
    *,
    open_field: str,
) -> None:
    for detector_id, entries in (detector_map or {}).items():
        if not entries:
            continue
        dict_entries = [entry for entry in entries if isinstance(entry, dict)]
        if dict_entries:
            for entry in dict_entries:
                _add_record_by_detector(records_by_detector, entry, default_detector_id=detector_id)
            continue
        # Legacy by_detector maps hold snippets only. Keep one placeholder per
        # detector so counts are not inflated, but do not invent a timestamp.
        _add_record_by_detector(
            records_by_detector,
            {"detector_ids": [detector_id], open_field: "Open"},
            default_detector_id=detector_id,
        )


def _freshest_record(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    with_ts = [(record, _record_timestamp(record)) for record in records]
    dated = [(record, ts) for record, ts in with_ts if ts is not None]
    if dated:
        return max(dated, key=lambda item: item[1])[0]
    return records[0] if records else {}


def _canonical_servicenow_record(detector_id: str, record: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(record)
    out.setdefault("detector_ids", [detector_id])
    out.setdefault("state", "Open")
    ts = _timestamp_value(out)
    if ts is not None and out.get("sys_created_on") is None:
        out["sys_created_on"] = ts
    return out


def _canonical_jira_record(detector_id: str, record: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(record)
    out.setdefault("detector_ids", [detector_id])
    out.setdefault("status", "Open")
    ts = _timestamp_value(out)
    if ts is not None and out.get("created") is None:
        out["created"] = ts
    return out


def build_corroboration_run_data(
    *,
    systems: Any,
    sn_by_detector: Dict[str, List[Any]],
    jira_by_detector: Dict[str, List[Any]],
    run_timestamp_iso: str,
    source_payloads: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Build the ENT-2 corroboration engine input from discovery run data.

    The builder is intentionally part of the engine module so runner, replay
    paths, tests, and future endpoints all construct the same input contract.
    It uses real record timestamps when matched records are available and never
    creates more than one ServiceNow/Jira corroboration record per detector.
    """
    connected = set(normalise_connected_systems(systems))
    payloads = [_dict_or_empty(p) for p in (source_payloads or []) if isinstance(p, dict)]

    sn_records_by_detector: Dict[str, List[Dict[str, Any]]] = {}
    for record in _extract_matched_records(
        payloads,
        ("lending_incidents", "matched_incidents", "strs_incidents"),
    ):
        _add_record_by_detector(sn_records_by_detector, record)
    _add_detector_map_records(sn_records_by_detector, sn_by_detector, open_field="state")

    jira_records_by_detector: Dict[str, List[Dict[str, Any]]] = {}
    for record in _extract_matched_records(
        payloads,
        ("lending_issues", "matched_issues", "strs_issues"),
    ):
        _add_record_by_detector(jira_records_by_detector, record)
    _add_detector_map_records(jira_records_by_detector, jira_by_detector, open_field="status")

    data: Dict[str, Any] = {
        "connected_systems": sorted(connected),
        "servicenow": {
            "incidents": [
                _canonical_servicenow_record(detector_id, _freshest_record(records))
                for detector_id, records in sorted(sn_records_by_detector.items())
                if records
            ]
        },
        "jira": {
            "issues": [
                _canonical_jira_record(detector_id, _freshest_record(records))
                for detector_id, records in sorted(jira_records_by_detector.items())
                if records
            ]
        },
    }

    if "confluence" in connected:
        confluence = _find_corroboration_block("confluence", payloads)
        if confluence:
            data["confluence"] = confluence
        elif payloads:
            logger.warning(
                "ENT-2 corroboration: Confluence is connected but no recognized "
                "corroboration block was found"
            )

    # SharePoint carries the same documentation-source shape as Confluence
    # ({activity, cross_references, estates}). Carried through so a rule can read
    # it; no rule consumes it today, so this changes no verdict on its own.
    if "sharepoint" in connected:
        sharepoint = _find_corroboration_block("sharepoint", payloads)
        if sharepoint:
            data["sharepoint"] = sharepoint

    if "slack" in connected:
        slack = _find_corroboration_block("slack", payloads)
        if slack:
            data["slack"] = slack

    # R17-A3 / COR-09: carry the Java-app operational-friction block through when
    # the Java application source is connected for this run.
    if "java_app" in connected:
        java_app = _find_corroboration_block("java_app", payloads)
        if java_app:
            data["java_app"] = java_app
    # R17-A4 / COR-10: same for the .NET application source (parallel to Java).
    if "dotnet_app" in connected:
        dotnet_app = _find_corroboration_block("dotnet_app", payloads)
        if dotnet_app:
            data["dotnet_app"] = dotnet_app
    if "teams" in connected:
        teams = _find_corroboration_block("teams", payloads)
        if teams:
            data["teams"] = teams

    return data


# ─────────────────────────────────────────────────────────────────────────────
# The eight rule check functions (ENT-2 Section 1a)
# Each is explicit and independently testable.
# ─────────────────────────────────────────────────────────────────────────────

def check_cor01_servicenow_team_incidents(
    detector_id: str,
    run_data: Dict[str, Any],
    run_timestamp: datetime,
) -> bool:
    """COR-01: ServiceNow shows an open incident for the same team within 30 days.

    Fires when at least one ServiceNow incident is open, within the
    corroboration window, and linked to this detector.
    """
    for incident in _servicenow_incidents(run_data):
        if (
            _linked_to_detector(incident, detector_id)
            and _is_open(incident)
            and _within_window(incident, run_timestamp)
        ):
            return True
    return False


def check_cor02_jira_process_issues(
    detector_id: str,
    run_data: Dict[str, Any],
    run_timestamp: datetime,
) -> bool:
    """COR-02: Jira shows unresolved issues for the same process within 30 days."""
    for issue in _jira_issues(run_data):
        # Exclude sprint-velocity-only signals (handled by COR-07).
        if (
            _linked_to_detector(issue, detector_id)
            and _is_open(issue)
            and _within_window(issue, run_timestamp)
        ):
            return True
    return False


def check_cor03_triple(cor01_fired: bool, cor02_fired: bool) -> bool:
    """COR-03: derived — true when BOTH COR-01 and COR-02 fired."""
    return bool(cor01_fired and cor02_fired)


def check_cor04_confluence_doc_gap(
    detector_id: str,
    run_data: Dict[str, Any],
) -> bool:
    """COR-04: COVENANT_TRACKING_GAP + Confluence shows no covenant documentation.

    Only applies to the COVENANT_TRACKING_GAP detector. Fires when Confluence
    data is present and explicitly reports no covenant documentation.
    """
    if detector_id != _COVENANT_DETECTOR:
        return False
    confluence = _system_block(run_data, "confluence")
    if not confluence:
        return False
    # Documentation gap = present flag is explicitly False.
    present = confluence.get("covenant_documentation_present")
    if present is None:
        # Alternative shape: explicit gap flag.
        return bool(confluence.get("documentation_gap", False))
    return present is False


#: Conversation sources subject to COR-05/06 (the MEDIUM corroboration ceiling).
#: Both Slack (R16-A2) and Teams (R17-A1 / AT-433) are noisy conversation sources:
#: their escalation pattern corroborates a finding but never elevates it ALONE —
#: it reaches HIGH only WITH a primary system-of-record corroborator. Teams reuses
#: the EXACT same rule IDs (COR-05 supporting / COR-06 with primary) as Slack — no
#: new rule or mechanism is introduced, so the apply_corroboration_confidence
#: COR-05-only clamp and the T3 ceiling clamp treat both identically.
_CONVERSATION_SOURCES: tuple[tuple[str, str], ...] = (
    ("slack", "Slack"),
    ("teams", "Teams"),
)


def check_cor05_conversation_escalation(
    run_data: Dict[str, Any],
    run_timestamp: datetime,
    source_id: str,
) -> bool:
    """COR-05/06 precondition: a conversation source's ESCALATION_PATTERN fired
    within the window.

    Source-agnostic over conversation sources (``slack`` / ``teams``): detects the
    escalation signal for ``source_id``. Whether it ELEVATES depends on a primary
    corroborator (decided in evaluate_corroboration):
      - conversation source alone        -> COR-05 (supporting only, no elevation)
      - conversation source + ServiceNow/Jira -> COR-06 (elevates)
    """
    block = _system_block(run_data, source_id)
    escalation = block.get("escalation_pattern")
    if not isinstance(escalation, dict) or not escalation:
        # Alternative shape: a boolean flag.
        return bool(block.get("escalation_pattern_fired", False))
    if not escalation.get("fired", False):
        return False
    # If a timestamp is present, enforce the window; if absent, accept (the
    # escalation pattern is computed for the current run).
    if _record_timestamp(escalation) is not None:
        return _within_window(escalation, run_timestamp)
    return True


def check_cor05_slack_escalation(
    run_data: Dict[str, Any],
    run_timestamp: datetime,
) -> bool:
    """COR-05/06 precondition for Slack — thin wrapper over the conversation-source
    check (kept for backward compatibility / Slack-specific callers)."""
    return check_cor05_conversation_escalation(run_data, run_timestamp, "slack")


def check_cor06_slack_with_primary(slack_fired: bool, primary_fired: bool) -> bool:
    """COR-06: Slack escalation AND a primary corroborator (COR-01 or COR-02)."""
    return bool(slack_fired and primary_fired)


def check_cor07_jira_sprint_velocity(
    detector_id: str,
    run_data: Dict[str, Any],
    run_timestamp: datetime,
) -> bool:
    """COR-07: LOAN_ORIGINATION_BOTTLENECK + Jira sprint velocity decline.

    Only applies to loan-origination detectors. Fires when Jira reports a
    sprint velocity decline within the window.
    """
    if detector_id not in _LOAN_ORIGINATION_DETECTORS:
        return False
    velocity = _system_block(run_data, "jira").get("sprint_velocity")
    if not isinstance(velocity, dict) or not velocity:
        return False
    if not velocity.get("declined", False):
        return False
    if _record_timestamp(velocity) is not None:
        return _within_window(velocity, run_timestamp)
    return True


def check_cor08_single_source(run_data: Dict[str, Any]) -> bool:
    """COR-08: only one system connected — cannot self-corroborate.

    Fires when the connected_systems list contains one or zero systems.
    """
    return len(_connected_systems(run_data)) <= 1


def check_cor09_java_app_operational(
    run_data: Dict[str, Any],
    run_timestamp: datetime,
) -> bool:
    """COR-09: a Java application shows operational friction within the window.

    R17-A3 §3: a Java-app operational signal (rising error rate, latency
    degradation, resource pressure, or a recurring exception cluster) corroborates
    a finding in another connected system — e.g. an error-rate rise corroborating
    a ServiceNow incident spike for the same service. Unlike the Slack ceiling
    (COR-05), this is an ELEVATING corroborator: operational signals are directly
    measured, so they are first-class observed evidence.

    Fires when the Java-app ``operational_friction`` block reports ``fired`` and,
    if it carries a timestamp, that timestamp is within the corroboration window.
    A friction block with no timestamp is accepted (it is computed for the current
    run), mirroring the Slack escalation check.
    """
    java = _system_block(run_data, "java_app")
    friction = java.get("operational_friction")
    if not isinstance(friction, dict) or not friction:
        # Alternative shape: a boolean flag.
        return bool(java.get("operational_friction_fired", False))
    if not friction.get("fired", False):
        return False
    if _record_timestamp(friction) is not None:
        return _within_window(friction, run_timestamp)
    return True


def check_cor10_dotnet_app_operational(
    run_data: Dict[str, Any],
    run_timestamp: datetime,
) -> bool:
    """COR-10: a .NET application shows operational friction within the window.

    R17-A4 §3: the .NET counterpart to COR-09. A .NET-app operational signal
    (rising error rate, latency degradation, resource pressure, or a recurring
    exception cluster) corroborates a finding in another connected system — e.g. a
    .NET error/latency rise corroborating a ServiceNow incident spike for the same
    service. Unlike the Slack ceiling (COR-05), this is an ELEVATING corroborator:
    operational signals are directly measured from the running application's
    logs/diagnostics, so they are first-class observed evidence — identical to Java,
    because the two share the same signal language and the same corroboration model.

    Fires when the .NET-app ``operational_friction`` block reports ``fired`` and, if
    it carries a timestamp, that timestamp is within the corroboration window. A
    friction block with no timestamp is accepted (it is computed for the current
    run), mirroring the Java / Slack checks.
    """
    dotnet = _system_block(run_data, "dotnet_app")
    friction = dotnet.get("operational_friction")
    if not isinstance(friction, dict) or not friction:
        # Alternative shape: a boolean flag.
        return bool(dotnet.get("operational_friction_fired", False))
    if not friction.get("fired", False):
        return False
    if _record_timestamp(friction) is not None:
        return _within_window(friction, run_timestamp)
    return True


def _weighting_info_for_system(
    weighting_context: Optional[Any],
    system_id: str,
) -> Dict[str, Any]:
    """Return role/priority/source_weight for one corroborating system.

    No context, neutral context, missing system config, or malformed context all
    fall back to neutral weight so older/non-Stack-Builder runs preserve their
    existing corroboration behavior.
    """
    info: Dict[str, Any] = {
        "system_id": system_id,
        "role": "",
        "priority": "",
        "configured": False,
        "source_weight": 1.0,
        "lead_authority": True,
    }

    if weighting_context is None or getattr(weighting_context, "is_neutral", True):
        return info

    try:
        weighting = weighting_context.get(system_id)
        source_weight = float(getattr(weighting, "source_weight", 1.0))
        role = str(getattr(weighting, "role", "") or "")
        priority = str(getattr(weighting, "priority", "") or "")
        configured = not bool(getattr(weighting, "is_neutral", False))
    except Exception:  # noqa: BLE001 - corroboration must remain non-blocking.
        return info

    info.update(
        {
            "role": role,
            "priority": priority,
            "configured": configured,
            "source_weight": round(source_weight, 4),
            "lead_authority": source_weight >= _CORROBORATION_LEAD_WEIGHT_MIN,
        }
    )
    return info


def _has_lead_authority(
    weighting_context: Optional[Any],
    system_id: str,
) -> bool:
    """True when a corroborating system may lead a HIGH elevation."""
    return bool(_weighting_info_for_system(weighting_context, system_id)["lead_authority"])


def _build_corroboration_weight_debug(
    fired_rules: List[str],
    weighting_context: Optional[Any],
) -> Dict[str, Any]:
    """Build R16-C1 debug details and decide whether HIGH elevation is allowed."""
    source_debug: Dict[str, Dict[str, Any]] = {}
    for rule_id in fired_rules:
        system_id = _CORROBORATION_RULE_SYSTEMS.get(rule_id)
        if not system_id:
            continue
        info = _weighting_info_for_system(weighting_context, system_id)
        existing = source_debug.get(system_id)
        if existing is None or info["source_weight"] > existing["source_weight"]:
            source_debug[system_id] = info

    lead_sources = [
        system_id for system_id, info in source_debug.items()
        if info["lead_authority"]
    ]
    total_weight = sum(float(info["source_weight"]) for info in source_debug.values())

    return {
        "applied": weighting_context is not None and not getattr(weighting_context, "is_neutral", True),
        "lead_weight_min": _CORROBORATION_LEAD_WEIGHT_MIN,
        "source_contributions": source_debug,
        "total_non_slack_source_weight": round(total_weight, 4),
        "lead_sources": lead_sources,
        "eligible_for_high": bool(lead_sources),
    }


# ─────────────────────────────────────────────────────────────────────────────
# 2.0-B2 T6 — cross-source identity gate (AC5)
# ─────────────────────────────────────────────────────────────────────────────

def _apply_identity_gate(
    org_id: str,
    fired_rules: List[str],
    run_data: Dict[str, Any],
    identity_resolver: Any,
) -> Any:
    """Run the cross-source identity gate over the fired rules.

    Extracts the entity each primary source's corroborating records reference and
    asks :mod:`app.corroboration_identity_gate` whether a claimed shared identity
    is genuinely resolved. Returns the gate outcome, or ``None`` when the gate is
    unavailable.

    Never raises: corroboration is advisory and must never break scoring, so an
    import or extraction failure degrades to "no gate" — logged, and visible as an
    empty ``identity_gate`` on the result rather than a silently ungated HIGH.
    """
    try:
        from .corroboration_identity_gate import (
            default_resolver,
            first_entity_ref,
            gate_cross_source_corroboration,
        )
    except Exception as exc:  # noqa: BLE001 — gate unavailable, never fatal.
        logger.warning(
            "2.0-B2 T6: cross-source identity gate unavailable (%s) — "
            "corroboration elevation is NOT identity-verified this run", exc,
        )
        return None

    try:
        left = first_entity_ref("servicenow", _servicenow_incidents(run_data))
        right = first_entity_ref("jira", _jira_issues(run_data))
        resolver = identity_resolver if identity_resolver is not None else default_resolver()
        return gate_cross_source_corroboration(
            org_id, fired_rules, left=left, right=right, resolver=resolver
        )
    except Exception as exc:  # noqa: BLE001 — see the docstring.
        logger.warning("2.0-B2 T6: identity gate evaluation failed: %s", exc)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Label builder
# ─────────────────────────────────────────────────────────────────────────────

def _build_label(fired_rules: List[str], triple: bool) -> Optional[str]:
    """Build the on-card corroboration label from the fired rules.

    Triple corroboration always wins the headline. Otherwise the highest-value
    elevating rule's label is used, falling back to a supporting-only label.
    """
    if not fired_rules:
        return None
    if triple:
        return TRIPLE_CORROBORATION_LABEL
    # Priority order for the headline label among non-derived rules.
    for rule_id in ("COR-06", "COR-01", "COR-02", "COR-04", "COR-07", "COR-09", "COR-10", "COR-05", "COR-08"):
        if rule_id in fired_rules:
            return RULE_CARD_LABELS.get(rule_id)
    return RULE_CARD_LABELS.get(fired_rules[0])


# ─────────────────────────────────────────────────────────────────────────────
# Public engine entry point
# ─────────────────────────────────────────────────────────────────────────────

def evaluate_corroboration(
    detector_id: str,
    pack_id: str,
    run_data: Dict[str, Any],
    run_timestamp: datetime,
    org_id: str,
    weighting_context: Optional[Any] = None,
    identity_resolver: Optional[Any] = None,
) -> CorroborationResult:
    """Evaluate all applicable corroboration rules for this detector.

    Rules are evaluated in order; every matching rule is recorded. The highest
    elevation wins. Always returns a CorroborationResult, regardless of whether
    any elevation occurred — so the scoring pipeline has a predictable shape.

    Parameters
    ----------
    detector_id:
        The detector that fired (e.g. ``'COVENANT_TRACKING_GAP'``).
    pack_id:
        The active pack id (engine is pack-agnostic; accepted for context
        and future rules).
    run_data:
        Aggregated connected-system data (see module docstring).
    run_timestamp:
        The run's timestamp; the corroboration window is relative to it.
    org_id:
        The org id (accepted for context / future org-scoped rules).
    weighting_context:
        Optional Stack Builder weighting context (R16-C1). When provided,
        per-system role and priority modulate whether fired corroboration
        rules may lead a HIGH elevation. Accepted as ``Any`` to avoid a hard
        import dependency from the app layer into discovery.
    identity_resolver:
        Optional override for the 2.0-B2 T6 cross-source identity resolver
        (AC5) — the callable that answers "are these two entity references the
        same real thing?". Defaults to the graph-backed resolver. Injected in
        tests so the gate can be exercised without a database.

    Never raises for normal data issues — defensive .get() throughout. The
    caller (scoring pipeline) additionally wraps this in try/except so any
    unexpected failure is non-blocking (ENT-2 AC10).
    """
    run_data = run_data or {}

    # R16-C1: weighting context is active inside corroboration. Role/priority
    # values below decide whether fired supporting rules may lead HIGH.
    if weighting_context is not None and not getattr(weighting_context, "is_neutral", True):
        try:
            w = weighting_context.get(detector_id.lower().split("_")[0])
            logger.debug(
                "R16-C1 corroboration: detector=%s weighting_context present "
                "(%d systems). Role/priority modulation active.",
                detector_id,
                len(getattr(weighting_context, "weightings", {})),
            )
            del w
        except Exception:  # noqa: BLE001 — never block corroboration
            pass

    if run_timestamp is None:
        run_timestamp = datetime.now(timezone.utc)
    elif run_timestamp.tzinfo is None:
        run_timestamp = run_timestamp.replace(tzinfo=timezone.utc)

    # COR-08: single source short-circuits — a lone system cannot self-corroborate.
    # Sources stay empty so the UI renders no badge (AC7).
    if check_cor08_single_source(run_data):
        return _empty_result(CONFIDENCE_MEDIUM)

    fired_rules: List[str] = []
    sources: List[str] = []

    # COR-01: ServiceNow incident for the same team.
    cor01 = check_cor01_servicenow_team_incidents(detector_id, run_data, run_timestamp)
    if cor01:
        fired_rules.append("COR-01")
        sources.append("ServiceNow")

    # COR-02: Jira issues for the same process.
    cor02 = check_cor02_jira_process_issues(detector_id, run_data, run_timestamp)
    if cor02:
        fired_rules.append("COR-02")
        sources.append("Jira")

    # COR-03: triple corroboration (derived from COR-01 AND COR-02).
    triple = check_cor03_triple(cor01, cor02)
    if triple:
        fired_rules.append("COR-03")

    # COR-04: Confluence documentation gap (COVENANT_TRACKING_GAP only).
    if check_cor04_confluence_doc_gap(detector_id, run_data):
        fired_rules.append("COR-04")
        sources.append("Confluence (no process documentation)")

    # COR-07: Jira sprint velocity decline (loan-origination only).
    if check_cor07_jira_sprint_velocity(detector_id, run_data, run_timestamp):
        fired_rules.append("COR-07")
        sources.append("Jira (sprint velocity decline)")

    # COR-09: Java application operational friction (R17-A3). First-class observed
    # evidence — an ELEVATING corroborator like ServiceNow/Jira (not Slack-capped).
    if check_cor09_java_app_operational(run_data, run_timestamp):
        fired_rules.append("COR-09")
        sources.append("Java application (operational signal)")

    # COR-10: .NET application operational friction (R17-A4). The .NET counterpart
    # to COR-09 — same observed-evidence basis, so it elevates identically.
    if check_cor10_dotnet_app_operational(run_data, run_timestamp):
        fired_rules.append("COR-10")
        sources.append(".NET application (operational signal)")

    # COR-05 / COR-06: conversation-source escalation (Slack, Teams).
    # A conversation source elevates ONLY with a primary corroborator
    # (ServiceNow/Jira). The conversation-source ceiling: alone it stays MEDIUM
    # (COR-05), never HIGH. Teams (R17-A1 / AT-433) reuses the SAME rule IDs and
    # ceiling as Slack (R16-A2) — no new mechanism — so the COR-05-only clamp in
    # apply_corroboration_confidence and the T3 ceiling clamp cap both identically.
    primary_fired = (
        (cor01 and _has_lead_authority(weighting_context, "servicenow")) or
        (cor02 and _has_lead_authority(weighting_context, "jira"))
    )
    for _src_id, _src_label in _CONVERSATION_SOURCES:
        if not check_cor05_conversation_escalation(run_data, run_timestamp, _src_id):
            continue
        if check_cor06_slack_with_primary(True, primary_fired):
            if "COR-06" not in fired_rules:
                fired_rules.append("COR-06")
            sources.append(f"{_src_label} (escalation pattern)")
        else:
            if "COR-05" not in fired_rules:      # supporting only — no elevation
                fired_rules.append("COR-05")
            sources.append(f"{_src_label} (supporting only)")

    # 2.0-B2 T6 (AC5): the cross-source identity gate. "Corroborated across
    # ServiceNow and Jira" only means something if both sides are about the SAME
    # thing — a shared name is not a shared identity. Where the two sources'
    # entity references claim one identity that is NOT genuinely resolved, the
    # cross-source rules keep their evidence but lose their elevation.
    gate_outcome = _apply_identity_gate(org_id, fired_rules, run_data, identity_resolver)
    identity_gate = gate_outcome.to_dict() if gate_outcome is not None else {}
    blocked_rules = set(gate_outcome.blocked_rules) if gate_outcome is not None else set()
    if blocked_rules:
        # The triple headline is a cross-source claim too, so it falls with the
        # pair it is derived from.
        triple = triple and "COR-03" not in blocked_rules

    # Determine elevation. R16-C1 source role/priority now shape corroboration
    # contribution, while COR-05 remains supporting-only.
    weight_debug = _build_corroboration_weight_debug(fired_rules, weighting_context)
    elevating_rules = [
        r for r in fired_rules
        if r != "COR-05" and r in _CORROBORATION_RULE_SYSTEMS
        and r not in blocked_rules
    ]
    elevated = (
        CONFIDENCE_HIGH
        if elevating_rules and weight_debug["eligible_for_high"]
        else CONFIDENCE_MEDIUM
    )
    original = CONFIDENCE_MEDIUM

    label = _build_label(fired_rules, triple)

    return CorroborationResult(
        rule_ids=fired_rules,
        corroboration_sources=sources,
        corroboration_label=label,
        elevated_confidence=elevated,
        original_confidence=original,
        triple_corroboration=triple,
        confidence_elevated=CONFIDENCE_ORDER[elevated] > CONFIDENCE_ORDER[original],
        elevation_target=elevated,
        corroboration_weight_debug=weight_debug,
        identity_gate=identity_gate,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Pipeline application helper (used by the scoring pipeline — T3)
# ─────────────────────────────────────────────────────────────────────────────

def apply_corroboration_confidence(
    scorer_confidence: Optional[str],
    result: CorroborationResult,
) -> str:
    """Return the final confidence after applying corroboration — NEVER downgrades.

    The corroboration engine may only raise confidence. If the scorer already
    assigned a higher (or equal) confidence than the corroboration verdict, the
    scorer's value is preserved. This protects existing pack behaviour
    (nCino/STRS already assign HIGH for several detectors).

    R16-C1 T3 — Hard ceiling defense-in-depth
    ------------------------------------------
    Before applying the elevation, this function enforces the Slack-only and
    single-source ceilings as a second line of defense against any future rule
    that might accidentally try to elevate beyond MEDIUM for those cases:

    • If only COR-05 (Slack-only, no primary corroborator) fired, the elevated
      confidence is capped at MEDIUM before comparison. COR-05 already sets
      ``elevated_confidence = MEDIUM`` — this guard catches any future drift.
    • If only COR-08 (single source) fired, the elevated confidence is capped
      at MEDIUM. COR-08 already returns an empty/MEDIUM result — guard for
      defense-in-depth.

    Neither guard can lower the scorer's own baseline (never-downgrade holds).
    """
    if scorer_confidence is None or not str(scorer_confidence).strip():
        raise ValueError("scorer_confidence is required before corroboration elevation")
    base = str(scorer_confidence).strip().upper()
    if base not in CONFIDENCE_ORDER:
        raise ValueError(f"unknown scorer confidence: {scorer_confidence!r}")

    # R16-C1 T3: enforce hard ceilings on the corroboration verdict before
    # comparing it against the scorer's baseline.
    elevated = result.elevated_confidence

    # Slack-only ceiling: if the only fired rule is COR-05 (or no elevating
    # rules at all because COR-05 is excluded from elevating_rules), the
    # elevation target cannot exceed MEDIUM.
    only_slack_rules = bool(result.rule_ids) and all(
        r in ("COR-05",) for r in result.rule_ids
    )
    if only_slack_rules and CONFIDENCE_ORDER.get(elevated, 0) > CONFIDENCE_ORDER[CONFIDENCE_MEDIUM]:
        elevated = CONFIDENCE_MEDIUM
        logger.warning(
            "R16-C1 T3: Slack-only ceiling enforced — corroboration verdict "
            "clamped from %s to MEDIUM (rules: %s)",
            result.elevated_confidence,
            result.rule_ids,
        )

    # Single-source ceiling: if only COR-08 fired, cap at MEDIUM.
    only_single_source = bool(result.rule_ids) and all(
        r in ("COR-08",) for r in result.rule_ids
    )
    if only_single_source and CONFIDENCE_ORDER.get(elevated, 0) > CONFIDENCE_ORDER[CONFIDENCE_MEDIUM]:
        elevated = CONFIDENCE_MEDIUM
        logger.warning(
            "R16-C1 T3: Single-source ceiling enforced — corroboration verdict "
            "clamped from %s to MEDIUM (rules: %s)",
            result.elevated_confidence,
            result.rule_ids,
        )

    base_rank = CONFIDENCE_ORDER[base]
    elevated_rank = CONFIDENCE_ORDER.get(elevated, base_rank)
    return elevated if elevated_rank > base_rank else base
