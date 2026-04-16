"""
D7 — CROSS_SYSTEM_ECHO

Fires when: MAX(sf_echo_score, sn_echo_score, jira_echo_score) > 0.15
            AND at least one source has volume >= 30 records

metric_value = MAX of the three echo scores.
signal_source reflects which system produced the dominant score.

SF-1.3 thresholds:
    echo_threshold: 0.15
    min_volume: 30
"""
from __future__ import annotations
from typing import Any, Dict, List, Tuple
from ..models import DetectorResult

DETECTOR_ID = "CROSS_SYSTEM_ECHO"
THRESHOLD = 0.15
MIN_VOLUME = 30


def _extract_scores(
    sf_data: Dict[str, Any],
    sn_data: Dict[str, Any],
    jira_data: Dict[str, Any],
) -> List[Tuple[str, float, int, dict]]:
    """Return list of (source, echo_score, volume, raw_evidence) tuples."""
    sources = []

    # Salesforce side
    sf_csr = sf_data.get("cross_system_references") or {}
    sf_score = float(sf_csr.get("sf_echo_score", 0.0))
    sf_vol = int(sf_csr.get("sf_total_cases", 0))
    if sf_vol > 0:
        sources.append(("salesforce", sf_score, sf_vol, {
            "sf_echo_count": int(sf_csr.get("sf_echo_count", 0)),
            "sf_total_cases": sf_vol,
            "sf_echo_score": sf_score,
            "sn_match_count": 0,
            "sn_total_incidents": 0,
            "sn_echo_score": 0.0,
            "matched_patterns": sf_csr.get("matched_patterns", []),
        }))

    # ServiceNow side
    sn_csr = (sn_data or {}).get("cross_system_references") or {}
    sn_score = float(sn_csr.get("sn_echo_score", 0.0))
    sn_vol = int(sn_csr.get("sn_total_incidents", 0))
    if sn_vol > 0:
        sources.append(("servicenow", sn_score, sn_vol, {
            "sf_echo_count": 0,
            "sf_total_cases": 0,
            "sf_echo_score": 0.0,
            "sn_match_count": int(sn_csr.get("sn_match_count", 0)),
            "sn_total_incidents": sn_vol,
            "sn_echo_score": sn_score,
            "matched_patterns": [sn_csr.get("matched_pattern", "CS-")],
        }))

    # Jira side
    jira_im = (jira_data or {}).get("issue_metrics") or {}
    jira_score = float(jira_im.get("jira_echo_score", 0.0))
    jira_vol = int(jira_im.get("total_issues_90d", 0))
    if jira_vol > 0:
        sources.append(("jira", jira_score, jira_vol, {
            "sf_echo_count": 0,
            "sf_total_cases": 0,
            "sf_echo_score": 0.0,
            "sn_match_count": 0,
            "sn_total_incidents": 0,
            "sn_echo_score": 0.0,
            "matched_patterns": ["CS-"],
            "jira_sf_label_count": int(jira_im.get("salesforce_label_count", 0)),
            "jira_total_issues": jira_vol,
            "jira_echo_score": jira_score,
        }))

    return sources


def detect(
    sf_data: Dict[str, Any],
    sn_data: Dict[str, Any] = None,
    jira_data: Dict[str, Any] = None,
) -> List[DetectorResult]:
    sources = _extract_scores(sf_data, sn_data or {}, jira_data or {})

    # Find the dominant source
    eligible = [
        (src, score, vol, ev)
        for src, score, vol, ev in sources
        if score > THRESHOLD and vol >= MIN_VOLUME
    ]

    if not eligible:
        return []

    # Use the highest echo score as the metric_value
    dominant = max(eligible, key=lambda x: x[1])
    dom_source, dom_score, _, _ = dominant

    # Merge all evidence into one dict for the full three-system picture
    merged_evidence: Dict[str, Any] = {
        "sf_echo_count": 0, "sf_total_cases": 0, "sf_echo_score": 0.0,
        "sn_match_count": 0, "sn_total_incidents": 0, "sn_echo_score": 0.0,
        "matched_patterns": [],
    }
    all_patterns = set()
    for _, _, _, ev in sources:
        for k in ("sf_echo_count", "sf_total_cases", "sn_match_count", "sn_total_incidents"):
            merged_evidence[k] = merged_evidence.get(k, 0) + ev.get(k, 0)
        for k in ("sf_echo_score", "sn_echo_score"):
            merged_evidence[k] = max(merged_evidence.get(k, 0.0), ev.get(k, 0.0))
        all_patterns.update(ev.get("matched_patterns", []))
        # Jira extras
        for jk in ("jira_sf_label_count", "jira_total_issues", "jira_echo_score"):
            if jk in ev:
                merged_evidence[jk] = ev[jk]

    merged_evidence["matched_patterns"] = sorted(all_patterns)

    return [DetectorResult(
        detector_id=DETECTOR_ID,
        signal_source=dom_source,
        metric_value=round(dom_score, 4),
        threshold=THRESHOLD,
        raw_evidence=merged_evidence,
    )]
