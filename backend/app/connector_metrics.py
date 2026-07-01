import logging
from typing import Any, Dict, List, Optional

from . import db

logger = logging.getLogger(__name__)


def _numeric_metric(raw: Dict[str, Any], key: str) -> Optional[int]:
    value = raw.get(key)
    return _numeric_value(value)


def _numeric_value(value: Any) -> Optional[int]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value))
        except ValueError:
            return None
    return None


def _input_metric(inputs: Dict[str, Any], key: str) -> int:
    return _numeric_value(inputs.get(key)) or 0


def _max_raw_metric(opps: List[Dict[str, Any]], key: str) -> int:
    values = [
        value
        for opp in opps
        if isinstance(opp.get("raw_evidence"), dict)
        for value in [_numeric_metric(opp["raw_evidence"], key)]
        if value is not None
    ]
    return max(values, default=0)


def _evidence_count_for_source(opps: List[Dict[str, Any]], source: str) -> int:
    count = 0
    seen: set[str] = set()
    for opp_index, opp in enumerate(opps):
        evidence_items = opp.get("evidence") or []
        if not isinstance(evidence_items, list):
            continue
        for ev_index, evidence in enumerate(evidence_items):
            if not isinstance(evidence, dict) or evidence.get("source") != source:
                continue
            evidence_id = evidence.get("id")
            dedupe_key = str(evidence_id) if evidence_id else f"{opp_index}:{ev_index}"
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            count += 1
    return count


def _metric_value(value: int) -> str:
    return str(value) if value > 0 else "\u2014"


def _update_connector(
    org_id: str,
    connector_id: str,
    metrics: List[Dict[str, str]],
    signal_strength: Optional[int] = None,
) -> None:
    # Tenant isolation (R17-D3 / AT-448): run-derived connector metrics belong to
    # the org that ran the discovery, not the shared catalog. Reading/writing the
    # raw catalog row leaked one org's run metrics (lastSynced / signalStrength /
    # metrics) into the Integration Hub cards of every other org. Read and write
    # through the org-namespaced connector overlay instead.
    connector = db.org_connector_get(org_id, connector_id)
    if not connector:
        return
    connector["metrics"] = metrics
    connector["lastSynced"] = "Just now"
    if signal_strength is not None:
        connector["signalStrength"] = signal_strength
    db.org_connector_set(org_id, connector_id, connector)


def update_connector_metrics_from_run(
    payload: Dict[str, Any], systems: List[str], org_id: str
) -> None:
    """
    Refresh Integration Hub metrics from the completed run payload.

    The seed data remains the initial display state; these values override it
    after a real run completes. This is deliberately non-blocking so connector
    metric persistence can never fail the discovery run itself.

    org_id scopes the write to the org that owns the run (R17-D3 / AT-448): the
    refreshed metrics are stored on that org's connector overlay only, so they
    never appear on another tenant's Integration Hub cards.
    """
    try:
        opps = payload.get("opportunities") or []
        if not isinstance(opps, list):
            opps = []

        inputs = payload.get("inputs") or {}
        if not isinstance(inputs, dict):
            inputs = {}

        system_set = {system.lower() for system in systems}

        # Detect pack from opportunities
        pack_ids = {o.get("packId", "") for o in opps if isinstance(o, dict)}
        is_strs = "strs_benefits" in pack_ids

        loans     = _max_raw_metric(opps, "total_loans")
        covenants = _max_raw_metric(opps, "total_covenants")

        # STRS PSS metrics
        applications = _max_raw_metric(opps, "total_applications")
        assignments  = _max_raw_metric(opps, "total_assignments")

        sf_evidence_count   = _evidence_count_for_source(opps, "Salesforce")
        sn_evidence_count   = _evidence_count_for_source(opps, "ServiceNow")
        jira_evidence_count = _evidence_count_for_source(opps, "Jira")

        sn_incident_count = (
            _input_metric(inputs, "sn_total_incidents_90d")
            or _max_raw_metric(opps, "sn_total_incidents")
            or sn_evidence_count
        )
        sn_signal_count = sn_evidence_count or _input_metric(inputs, "sn_lending_signal_count")

        jira_issue_count = (
            _input_metric(inputs, "jira_total_issues_90d")
            or _max_raw_metric(opps, "jira_total_issues")
            or jira_evidence_count
        )
        jira_signal_count = jira_evidence_count or _input_metric(inputs, "jira_lending_signal_count")

        signal_label = "Benefit signals" if is_strs else "Lending signals"

        if "salesforce" in system_set:
            if is_strs:
                _update_connector(
                    org_id,
                    "salesforce",
                    [
                        {"label": "Applications", "value": _metric_value(applications)},
                        {"label": "Benefit Assignments", "value": _metric_value(assignments)},
                    ],
                    signal_strength=min(100, sf_evidence_count * 20) if sf_evidence_count else None,
                )
            else:
                _update_connector(
                    org_id,
                    "salesforce",
                    [
                        {"label": "Loans", "value": _metric_value(loans)},
                        {"label": "Covenants", "value": _metric_value(covenants)},
                    ],
                    signal_strength=min(100, sf_evidence_count * 20) if sf_evidence_count else None,
                )

        if "servicenow" in system_set:
            _update_connector(
                org_id,
                "servicenow",
                [
                    {"label": "Incidents (90d)", "value": _metric_value(sn_incident_count)},
                    {"label": signal_label, "value": _metric_value(sn_signal_count)},
                ],
            )

        if "jira" in system_set or "jira_confluence" in system_set:
            _update_connector(
                org_id,
                "jira_confluence",
                [
                    {"label": "Issues (90d)", "value": _metric_value(jira_issue_count)},
                    {"label": signal_label, "value": _metric_value(jira_signal_count)},
                ],
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Connector metrics update failed (non-blocking): %s", exc)
