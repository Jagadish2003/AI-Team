"""
SHARED_CI_HOTSPOT — MSP-B6 T3 (AT-738), Cloud-Operations pack.

The "expansion" finding no single-system tool can produce: incidents across
MULTIPLE services whose CIs traverse — via MSP-B3 dependency edges — to ONE
common CI, with event corroboration on that CI in the same windows. Twelve
services' pain, one shared storage tier.

Traversal uses the SHARED CI-dependency engine (``detectors.ci_dependency_graph``) —
the same adjacency construction and the same bounded BFS the MSP-B12 SecOps
concentration detector uses, so one CMDB can never yield two different notions of
"reachable within N hops". See that module for why it is not the LLM-oriented
``app.graph_context_builder``.

Traversal is DEPTH-BOUNDED (default 2 hops, configurable via T1's schema —
``thresholds.shared_ci_hotspot``): a service contributes to a common CI only when
that CI is reachable within ``max_hops`` dependency hops of the service's own CI.
A CI reachable only beyond the bound does not count (T3 AC2). Firing also requires
at least ``min_services`` distinct services AND (when configured) event
corroboration on the common CI within the window (T3 AC1/AC2).

Sources & confidence shape (MSP-B6 §1): graph traversal (observed B3 edges) +
ITSM + events — corroborated, window-gated, HIGH-eligible.

GUARDRAIL (T3 AC3): the finding wording is concentration-shaped
("incidents concentrate on a shared dependency ..."), NEVER causal
("... caused by ..."). Wording is produced by ``cloud_ops_finding``'s template
and validated by its causal gate — enforced in code, not by review.

References only services/CIs — never an individual (MSP-B6 AC4/AC7).

Input (read from ``sn_data['cloud_ops']``):
  ci_graph: {"edges": [{"from": <ci>, "to": <ci>}, ...]}   # B3 dependency edges
  service_incidents: [{service, ci, incident_ids: [...], incident_count}]
  event_signatures: [{signature, ci, window_overlap, event_count}]  # CI-level events
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..models import DetectorResult, make_detector_evaluation
from ..packs import cloud_ops_finding as fc
from .ci_dependency_graph import build_adjacency, reachable_within

try:
    from ..packs.cloud_ops_config import get_detector_thresholds
except Exception:  # pragma: no cover
    get_detector_thresholds = None  # type: ignore

DETECTOR_ID = "SHARED_CI_HOTSPOT"

DEFAULT_MAX_HOPS = 2
DEFAULT_MIN_SERVICES = 3
DEFAULT_REQUIRE_EVENT_CORROBORATION = True
_THRESHOLD_SECTION = "shared_ci_hotspot"

SIGNAL_METRICS: List[str] = [
    "service_count",     # metric_value — distinct services concentrating on the CI
    "incident_count",    # incidents across those services
    "max_hops",          # traversal bound applied
    "common_ci_count",   # shared-dependency CIs found
]


def _thresholds() -> Dict[str, Any]:
    defaults = {
        "max_hops": DEFAULT_MAX_HOPS,
        "min_services": DEFAULT_MIN_SERVICES,
        "require_event_corroboration": DEFAULT_REQUIRE_EVENT_CORROBORATION,
    }
    if get_detector_thresholds is None:
        return defaults
    return get_detector_thresholds(_THRESHOLD_SECTION, defaults)


def _adjacency(block: Dict[str, Any]) -> Dict[str, List[str]]:
    """Directed dependency graph: ``adj[from]`` = CIs that ``from`` depends on.

    Reads this pack's B3 edge location (``ci_graph.edges``, or the flat
    ``ci_dependency_edges``) and hands the edges to the SHARED
    :func:`~discovery.detectors.ci_dependency_graph.build_adjacency` — the same
    construction the SecOps pack's ``cmdb_adjacency`` uses, so both packs derive the
    identical graph (including the ``used_by``/``hosts``/``runs`` direction
    normalisation this detector previously lacked) from the same CMDB.
    """
    edges = ((block.get("ci_graph") or {}).get("edges")) or block.get("ci_dependency_edges") or []
    return build_adjacency(edges)


#: The depth-bounded walk is the SHARED one — see ``ci_dependency_graph``. The bound
#: itself (T3-AC2: a CI reachable only beyond ``max_hops`` does not count) is that
#: function's documented guarantee, exercised by both packs' detector tests.
_reachable_within = reachable_within


def _event_index(block: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Index window-overlapping events by the CI they were observed on."""
    index: Dict[str, Dict[str, Any]] = {}
    for ev in block.get("event_signatures") or []:
        if not isinstance(ev, dict):
            continue
        ci = ev.get("ci") or ev.get("configuration_item")
        if not ci:
            continue
        if bool(ev.get("window_overlap", ev.get("window_gated", False))):
            index.setdefault(str(ci), ev)
    return index


def _hotspots(block: Dict[str, Any], t: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return one hotspot descriptor per qualifying common CI."""
    max_hops = int(t.get("max_hops", DEFAULT_MAX_HOPS))
    min_services = int(t.get("min_services", DEFAULT_MIN_SERVICES))
    require_events = bool(t.get("require_event_corroboration", DEFAULT_REQUIRE_EVENT_CORROBORATION))

    adj = _adjacency(block)
    events = _event_index(block)
    service_incidents = [s for s in (block.get("service_incidents") or []) if isinstance(s, dict)]

    # common_ci -> {service_name: {hops, incident_count, incident_ids, ci}}
    concentration: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for si in service_incidents:
        service = str(si.get("service") or si.get("service_name") or "")
        svc_ci = str(si.get("ci") or si.get("configuration_item") or "")
        if not service or not svc_ci:
            continue
        reachable = _reachable_within(adj, svc_ci, max_hops)
        for common_ci, hops in reachable.items():
            entry = concentration.setdefault(common_ci, {})
            # keep the shortest hop if a service reaches the CI more than one way
            prev = entry.get(service)
            if prev is None or hops < prev["hops"]:
                entry[service] = {
                    "service": service,
                    "service_ci": svc_ci,
                    "hops": hops,
                    "incident_count": int(si.get("incident_count", len(si.get("incident_ids") or [])) or 0),
                    "incident_ids": [str(i) for i in (si.get("incident_ids") or [])],
                }

    hotspots: List[Dict[str, Any]] = []
    for common_ci, services in concentration.items():
        if len(services) < min_services:
            continue
        event = events.get(common_ci)
        if require_events and event is None:
            continue  # AC2: no event corroboration within window → does not fire
        hotspots.append({
            "common_ci": common_ci,
            "services": sorted(services.values(), key=lambda s: s["service"]),
            "event": event,
        })
    # Deterministic ordering: widest concentration first, then CI name.
    hotspots.sort(key=lambda h: (-len(h["services"]), h["common_ci"]))
    return hotspots


def _build_result(hotspot: Dict[str, Any], t: Dict[str, Any]) -> DetectorResult:
    common_ci = hotspot["common_ci"]
    services = hotspot["services"]
    event = hotspot["event"]
    min_services = int(t.get("min_services", DEFAULT_MIN_SERVICES))
    max_hops = int(t.get("max_hops", DEFAULT_MAX_HOPS))

    service_names = [s["service"] for s in services]
    incident_count = sum(int(s["incident_count"]) for s in services)
    service_count = len(services)

    statement = fc.build_concentration_statement(
        service_count=service_count,
        common_ci=common_ci,
        incident_count=incident_count,
    )

    artifacts: List[Dict[str, Any]] = [
        {"type": "shared_ci", "id": common_ci},
    ]
    for s in services:
        artifacts.append({
            "type": "service",
            "id": s["service"],
            "ci": s["service_ci"],
            "hops_to_shared_ci": s["hops"],
        })
        for inc in s["incident_ids"]:
            artifacts.append({"type": "incident", "id": inc, "service": s["service"]})
    if event is not None:
        artifacts.append({"type": "event_signature", "id": str(event.get("signature", "")), "ci": common_ci})

    evidence = {
        "statement": statement,
        "service_count": service_count,
        "services": service_names,
        "common_ci": common_ci,
        "incident_count": incident_count,
        "max_hops": max_hops,
        "hops_by_service": {s["service"]: s["hops"] for s in services},
        "event_corroborated": event is not None,
    }

    systems = ["servicenow", "events", "graph"]
    confidence = fc.build_confidence(
        fc.CONFIDENCE_HIGH,
        capped=False,
        eligible_for_high=True,
        note=(
            "Graph traversal (observed B3 edges) + ITSM + event corroboration on "
            "the shared CI within the same window."
        ),
    )
    corroboration = fc.build_corroboration(
        fc.STATUS_CORROBORATED,
        sources=systems,
        label="Incidents concentrate on a shared dependency (event-corroborated, window-gated)",
        window_gated=True,
    )

    contract = fc.build_finding_contract(
        evidence=evidence,
        confidence=confidence,
        corroboration=corroboration,
        source_trace=fc.build_source_trace(systems=systems, artifacts=artifacts),
    )
    # Belt-and-braces: the whole contract must stay concentration-shaped (AC3).
    fc.assert_not_causal(statement)
    fc.assert_not_causal(corroboration["label"])

    return DetectorResult(
        detector_id=DETECTOR_ID,
        signal_source="servicenow",
        metric_value=float(service_count),
        threshold=float(min_services),
        raw_evidence={
            "service_count": service_count,
            "incident_count": incident_count,
            "common_ci": common_ci,
            "max_hops": max_hops,
            "statement": statement,
            "confidence": confidence["level"],
            "corroborated": True,
            "corroboration_sources": systems,
            "finding_contract": contract,
        },
    )


def evaluate(
    sf_data: Optional[Dict[str, Any]] = None,
    sn_data: Optional[Dict[str, Any]] = None,
    jira_data: Optional[Dict[str, Any]] = None,
):
    block = (sn_data or {}).get("cloud_ops", {}) or {}
    t = _thresholds()
    hotspots = _hotspots(block, t)
    top = hotspots[0] if hotspots else None
    top_service_count = len(top["services"]) if top else 0
    top_incident_count = sum(int(s["incident_count"]) for s in top["services"]) if top else 0
    return make_detector_evaluation(
        module_name=__name__,
        detector_id=DETECTOR_ID,
        signal_source="servicenow",
        metric_value=float(top_service_count),
        threshold=float(t.get("min_services", DEFAULT_MIN_SERVICES)),
        fired=bool(hotspots),
        raw_evidence={
            "service_count": top_service_count,
            "incident_count": top_incident_count,
            "max_hops": int(t.get("max_hops", DEFAULT_MAX_HOPS)),
            "common_ci_count": len(hotspots),
        },
    )


def detect(
    sf_data: Optional[Dict[str, Any]] = None,
    sn_data: Optional[Dict[str, Any]] = None,
    jira_data: Optional[Dict[str, Any]] = None,
) -> List[DetectorResult]:
    block = (sn_data or {}).get("cloud_ops", {}) or {}
    t = _thresholds()
    return [_build_result(h, t) for h in _hotspots(block, t)]
