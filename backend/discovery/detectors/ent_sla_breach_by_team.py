"""
ENT-5 / AT-264 (T3) — ENT_SLA_BREACH_BY_TEAM detector.

Enterprise Operations Intelligence Pack — cross-system finding.

The finding: SLA breaches in ServiceNow are not evenly distributed. One or two
assignment teams are responsible for the majority of breaches. ServiceNow alone
shows the overall SLA breach rate; Jira shows which teams carry the heaviest
backlog. Combining them reveals which specific team is the constraint — the
finding an operations director can act on in the next staff meeting.

Signal:
    ServiceNow incidents carrying an SLA-breach flag are grouped by
    assignment_group. The detector identifies the team responsible for the
    largest share of breaches and measures both its share of org-wide breaches
    and its own breach rate.
        top_team_breach_pct  = top team's breaches / all SLA breaches
        top_team_breach_rate = top team's breaches / top team's own tickets

Fires when (AC4):
    top_team_breach_pct >= 0.40
    AND top_team_breach_rate >= 0.25
    AND teams_analysed >= 3

Cross-system team matching (AC5):
    The top team is matched to its Jira backlog. When an ENT-1 entity overlay is
    configured, ServiceNow assignment_group names and Jira team names are
    resolved to the same Team entity (so "Commercial Credit" and "Comm Credit
    Team" match). Without an overlay the detector falls back to exact
    (normalised) name comparison.

Confidence:
    MEDIUM standalone. Elevates to HIGH only when the top team resolves to a
    Team entity in the knowledge graph AND that entity has a high Jira open-issue
    count — i.e. ServiceNow breach concentration corroborated by Jira backlog.
    The exact-name fallback never elevates above MEDIUM (it is the degraded
    path). Confidence is surfaced in raw_evidence so the enterprise_ops scorer
    (ENT-5 T4/T5) can apply it; the ENT-2 corroboration engine wiring is T5.

metric_value = top_team_breach_pct (float, e.g. 0.62 = one team responsible for
62% of all SLA breaches).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from ..models import (
    DetectorResult,
    detector_result_from_evaluation,
    make_detector_evaluation,
)

DETECTOR_ID = "ENT_SLA_BREACH_BY_TEAM"

# Thresholds (Section 1c of ENT-5).
TOP_TEAM_BREACH_PCT_THRESHOLD = 0.40
TOP_TEAM_BREACH_RATE_THRESHOLD = 0.25
MIN_TEAMS_ANALYSED = 3

# A team carrying at least this many open Jira issues counts as "high backlog"
# for cross-system corroboration (AC5).
HIGH_JIRA_OPEN_ISSUES = 20

# Backward-compatible alias used by detector-specific branch tests.
THRESHOLD = TOP_TEAM_BREACH_PCT_THRESHOLD

CONFIDENCE_STANDALONE = "MEDIUM"
CONFIDENCE_CORROBORATED = "HIGH"

SIGNAL_METRICS: List[str] = [
    "top_team_breach_pct",   # metric_value — top team's share of all breaches
    "top_team_breach_rate",  # top team's own ticket breach rate
    "top_team_name",         # the responsible team (string identifier — see note)
    "org_breach_rate",       # organisation-wide breach rate
    "teams_analysed",        # number of assignment teams analysed
]
# Note: top_team_name is the actionable identifier for this finding and is
# deliberately included in SIGNAL_METRICS per ENT-5 §1c. It is a string, so the
# temporal snapshotter's is_signal_metric_value() skips it for numeric history
# capture while it remains available in raw_evidence; every other SIGNAL_METRIC
# is numeric and present in raw_evidence (AC6).


# ── helpers ──────────────────────────────────────────────────────────────────

def _norm(name: Any) -> str:
    """Normalise a team name for comparison: lowercased, whitespace-collapsed."""
    return " ".join(str(name or "").strip().lower().split())


def _team_breach_counts(sn_data: Optional[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], bool]:
    """Return per-team (team, total_tickets, breached) rows and the degraded flag.

    Accepts either a pre-grouped ``teams`` list or a raw ``incidents`` list
    (grouped here by assignment_group / SLA-breach flag).
    """
    block = (sn_data or {}).get("sla_breach_by_team") or {}
    degraded = bool(block.get("degraded_signal", False))

    teams = block.get("teams")
    if isinstance(teams, list) and teams:
        rows = []
        for t in teams:
            t = t or {}
            name = t.get("team") or t.get("assignment_group") or t.get("name")
            if not name:
                continue
            rows.append({
                "team": str(name),
                "total_tickets": int(t.get("total_tickets", t.get("total", 0)) or 0),
                "breached": int(t.get("breached", t.get("breach_count", 0)) or 0),
            })
        return rows, degraded

    # Group raw incidents by assignment_group.
    grouped: Dict[str, Dict[str, Any]] = {}
    for inc in block.get("incidents") or []:
        inc = inc or {}
        name = inc.get("assignment_group") or inc.get("team")
        if not name:
            continue
        breached = bool(
            inc.get("sla_breached")
            or inc.get("breached")
            or inc.get("sla_breach")
            or (inc.get("made_sla") is False)
        )
        row = grouped.setdefault(str(name), {"team": str(name), "total_tickets": 0, "breached": 0})
        row["total_tickets"] += 1
        if breached:
            row["breached"] += 1
    return list(grouped.values()), degraded


def _resolve_overlay(sn_data: Optional[Dict[str, Any]], jira_data: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """Return the normalised ENT-1 team entity overlay {team_name -> entity_id}.

    The overlay may be supplied on either payload. Returns {} when no overlay is
    configured (the detector then falls back to exact-name comparison).
    """
    overlay = (
        (sn_data or {}).get("team_entity_overlay")
        or (jira_data or {}).get("team_entity_overlay")
        or {}
    )
    if not isinstance(overlay, dict):
        return {}
    return {_norm(k): str(v) for k, v in overlay.items() if v}


def _jira_open_by_team(jira_data: Optional[Dict[str, Any]]) -> Dict[str, int]:
    """Return normalised {team_name -> open_issue_count} from the Jira payload."""
    backlog = (jira_data or {}).get("team_backlog") or {}
    by_team: Dict[str, int] = {}

    mapping = backlog.get("open_issues_by_team")
    if isinstance(mapping, dict):
        for name, count in mapping.items():
            by_team[_norm(name)] = by_team.get(_norm(name), 0) + int(count or 0)

    rows = backlog.get("teams")
    if isinstance(rows, list):
        for t in rows:
            t = t or {}
            name = t.get("team") or t.get("name")
            if not name:
                continue
            count = int(t.get("open_issues", t.get("open_issue_count", 0)) or 0)
            by_team[_norm(name)] = by_team.get(_norm(name), 0) + count

    return by_team


def corroborate_top_team(
    top_team_name: str,
    sn_data: Optional[Dict[str, Any]] = None,
    jira_data: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Cross-system match the top team to its Jira backlog (AC5).

    Returns a dict with:
      team_entity_resolved   — top team resolved to a Team entity via overlay
      top_team_jira_open_issues — open Jira issue count for the matched team
      jira_corroborated      — open count >= HIGH_JIRA_OPEN_ISSUES
      match_strategy         — "entity_graph" | "exact_name" | "none"
      confidence             — HIGH only when entity-resolved AND corroborated
    """
    overlay = _resolve_overlay(sn_data, jira_data)
    jira_open = _jira_open_by_team(jira_data)
    norm_top = _norm(top_team_name)

    team_entity_resolved = False
    open_issues = 0
    strategy = "none"

    top_entity = overlay.get(norm_top) if overlay else None
    if top_entity:
        # Sum Jira open issues for every Jira team resolving to the same entity.
        team_entity_resolved = True
        strategy = "entity_graph"
        for jira_team_norm, count in jira_open.items():
            if overlay.get(jira_team_norm) == top_entity:
                open_issues += count
        # If no Jira team name maps through the overlay, fall back to a direct
        # name hit so a configured-but-unmapped Jira side still corroborates.
        if open_issues == 0 and norm_top in jira_open:
            open_issues = jira_open[norm_top]
    elif norm_top in jira_open:
        # Exact (normalised) name comparison — overlay not configured/no match.
        strategy = "exact_name"
        open_issues = jira_open[norm_top]

    jira_corroborated = open_issues >= HIGH_JIRA_OPEN_ISSUES
    confidence = (
        CONFIDENCE_CORROBORATED
        if (team_entity_resolved and jira_corroborated)
        else CONFIDENCE_STANDALONE
    )

    return {
        "team_entity_resolved": team_entity_resolved,
        "top_team_jira_open_issues": open_issues,
        "jira_corroborated": jira_corroborated,
        "match_strategy": strategy,
        "confidence": confidence,
    }


# ── core computation ─────────────────────────────────────────────────────────

def _compute_metrics(
    sn_data: Optional[Dict[str, Any]],
    jira_data: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Compute SLA-breach concentration metrics and cross-system corroboration."""
    rows, degraded = _team_breach_counts(sn_data)
    teams_analysed = len(rows)

    total_breaches = sum(r["breached"] for r in rows)
    total_tickets = sum(r["total_tickets"] for r in rows)
    org_breach_rate = (total_breaches / total_tickets) if total_tickets else 0.0

    # Top team = the assignment group responsible for the most SLA breaches.
    top = max(rows, key=lambda r: r["breached"], default=None)
    if top is None or total_breaches == 0:
        top_team_name = ""
        top_team_breach_pct = 0.0
        top_team_breach_rate = 0.0
    else:
        top_team_name = top["team"]
        top_team_breach_pct = top["breached"] / total_breaches
        top_team_breach_rate = (
            top["breached"] / top["total_tickets"] if top["total_tickets"] else 0.0
        )

    corroboration = corroborate_top_team(top_team_name, sn_data, jira_data)

    return {
        "top_team_breach_pct": round(top_team_breach_pct, 4),
        "top_team_breach_rate": round(top_team_breach_rate, 4),
        "top_team_name": top_team_name,
        "org_breach_rate": round(org_breach_rate, 4),
        "teams_analysed": teams_analysed,
        "total_breaches": total_breaches,
        "total_tickets": total_tickets,
        "degraded_signal": degraded,
        **corroboration,
    }


def evaluate(
    sf_data: Optional[Dict[str, Any]] = None,
    sn_data: Optional[Dict[str, Any]] = None,
    jira_data: Optional[Dict[str, Any]] = None,
):
    """Evaluate SLA-breach concentration and return a DetectorEvaluation.

    Matches the runner convention ``evaluate(sf_data, sn_data, jira_data)``. SLA
    breach data is read from ``sn_data``; Jira backlog + the ENT-1 team overlay
    from ``jira_data`` (or ``sn_data``); the ``sf_data`` slot is compatibility-only.
    """
    metrics = _compute_metrics(sn_data, jira_data)

    fired = (
        not metrics["degraded_signal"]
        and metrics["teams_analysed"] >= MIN_TEAMS_ANALYSED
        and metrics["top_team_breach_pct"] >= TOP_TEAM_BREACH_PCT_THRESHOLD
        and metrics["top_team_breach_rate"] >= TOP_TEAM_BREACH_RATE_THRESHOLD
    )

    return make_detector_evaluation(
        module_name=__name__,
        detector_id=DETECTOR_ID,
        signal_source="servicenow",
        metric_value=metrics["top_team_breach_pct"],
        threshold=TOP_TEAM_BREACH_PCT_THRESHOLD,
        fired=fired,
        raw_evidence=metrics,
    )


def detect(
    sf_data: Optional[Dict[str, Any]] = None,
    sn_data: Optional[Dict[str, Any]] = None,
    jira_data: Optional[Dict[str, Any]] = None,
) -> List[DetectorResult]:
    """Return a list with one DetectorResult when the detector fires, else []."""
    evaluation = evaluate(sf_data, sn_data, jira_data)
    return [detector_result_from_evaluation(evaluation)] if evaluation.fired else []