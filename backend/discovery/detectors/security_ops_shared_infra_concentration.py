"""
SECOPS_SHARED_INFRA_CONCENTRATION — MSP-B12 T2, Security Operations pack.

The demo's sharpest moment: remediation workload across MULTIPLE services tracing —
via MSP-B3's explicit CMDB dependency edges — to ONE common underlying CI. Remediate
one shared component, relieve many services' toil. Reuses MSP-B6's hotspot traversal
(depth-bounded BFS over observed dependency edges), pointed at MSP-B11's VR workload.

Consumes ONLY ``sn_data['vulnerability_response']['vulnerable_items']`` (workload,
each attributed to its origin CI) and ``sn_data['cmdb']`` (MSP-B3 CIs + directed
depends-on edges). Traversal is DEPTH-BOUNDED (``max_hops``, default 2): a common CI
counts only when reachable within the bound from a workload-bearing CI. Firing needs
at least ``min_services`` distinct workload-bearing CIs converging on the same common
CI.

GUARDRAIL (AC3): the wording is CONCENTRATION-shaped ("workload across N services
concentrates on a shared dependency"), NEVER causal — validated in code by the
inherited causal gate (``assert_not_causal``). And it DOES NOT FIRE without a
dependency path: no CMDB edges, or nothing converging within the bound → no finding.

Described by CI CLASS + counts, never an individual host×vulnerability pair; the
specific CIs are reachable only through the source-trace evidence pointers.
"""
from __future__ import annotations

import hashlib
import json
from collections import deque
from typing import Any, Dict, List, Mapping, Optional, Tuple

from ..models import DetectorResult, make_detector_evaluation
from ..packs import security_ops_finding as fc
from . import security_ops_common as common

try:
    from ..packs.security_ops_config import get_detector_thresholds
except Exception:  # pragma: no cover
    get_detector_thresholds = None  # type: ignore

DETECTOR_ID = "SECOPS_SHARED_INFRA_CONCENTRATION"

DEFAULT_MAX_HOPS = 2
DEFAULT_MIN_SERVICES = 3
DEFAULT_REQUIRE_DEPENDENCY_PATH = True
_THRESHOLD_SECTION = "shared_infrastructure_concentration"

SIGNAL_METRICS: List[str] = [
    "service_count",       # metric_value — distinct workload-bearing CIs converging
    "workload_count",      # vulnerable items across those services
    "max_hops",
    "hotspot_count",
]


def _thresholds() -> Dict[str, Any]:
    defaults = {
        "max_hops": DEFAULT_MAX_HOPS,
        "min_services": DEFAULT_MIN_SERVICES,
        "require_dependency_path": DEFAULT_REQUIRE_DEPENDENCY_PATH,
    }
    if get_detector_thresholds is None:
        return defaults
    return get_detector_thresholds(_THRESHOLD_SECTION, defaults)


def _origin_ci(item: Mapping[str, Any]) -> Optional[str]:
    """The CI a vulnerable item's workload is attributed to (opaque sys_id)."""
    resolved = item.get("resolved_ci")
    if isinstance(resolved, Mapping):
        ci = common._text(resolved.get("source_record_id") or resolved.get("ci_sys_id"))
        if ci:
            return ci
    return common._text(item.get("ci_sys_id") or item.get("cmdb_ci"))


def _reachable_within(adj: Dict[str, List[str]], start: str, max_hops: int) -> Dict[str, int]:
    """BFS from ``start`` along depends-on edges; {ci: shortest_hop} for 1..max_hops.

    Excludes ``start`` — the concentration target is a downstream shared DEPENDENCY.
    """
    reached: Dict[str, int] = {}
    queue: deque[Tuple[str, int]] = deque([(start, 0)])
    seen = {start}
    while queue:
        node, hops = queue.popleft()
        if hops >= max_hops:
            continue
        for nxt in adj.get(node, []):
            if nxt in seen:
                continue
            seen.add(nxt)
            reached[nxt] = hops + 1
            queue.append((nxt, hops + 1))
    return reached


def _hotspots(sn_data: Optional[Mapping[str, Any]], org_id: Optional[str] = None) -> List[Dict[str, Any]]:
    vr = common.vr_block(sn_data)
    cmdb = common.cmdb_block(sn_data)
    effective = common.effective_org(vr, org_id) or common.effective_org(cmdb, org_id)

    adj = common.cmdb_adjacency(cmdb)
    if not adj:
        return []  # AC3: no dependency path → never fires
    class_index = common.ci_class_index(cmdb)

    t = _thresholds()
    max_hops = int(t.get("max_hops", DEFAULT_MAX_HOPS) or DEFAULT_MAX_HOPS)
    min_services = int(t.get("min_services", DEFAULT_MIN_SERVICES) or DEFAULT_MIN_SERVICES)

    # Workload-bearing origin CIs → their contributing vulnerable items.
    workload: Dict[str, List[Mapping[str, Any]]] = {}
    for item in common._records(vr, "vulnerable_items"):
        if not common.in_org(item, effective):
            continue
        origin = _origin_ci(item)
        if origin:
            workload.setdefault(origin, []).append(item)
    if len(workload) < min_services:
        return []

    # common_ci → {origin_ci: {hops, items}}
    concentration: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for origin, items in workload.items():
        for common_ci, hops in _reachable_within(adj, origin, max_hops).items():
            entry = concentration.setdefault(common_ci, {})
            prev = entry.get(origin)
            if prev is None or hops < prev["hops"]:
                entry[origin] = {"hops": hops, "items": items}

    hotspots: List[Dict[str, Any]] = []
    for common_ci, services in concentration.items():
        if len(services) < min_services:
            continue
        workload_items = [it for svc in services.values() for it in svc["items"]]
        hotspots.append({
            "common_ci": common_ci,
            "common_ci_class": class_index.get(common_ci, "unclassified"),
            "service_count": len(services),
            "service_ci_classes": sorted({class_index.get(o, "unclassified") for o in services}),
            "workload_count": len(workload_items),
            "max_hop_observed": max(s["hops"] for s in services.values()),
            "hops_bound": max_hops,
            "workload_items": workload_items,
            "org_id": effective,
        })
    hotspots.sort(key=lambda h: (-h["service_count"], -h["workload_count"], h["common_ci"]))
    return hotspots


def _statement(service_count: int, common_ci_class: str, workload_count: int) -> str:
    """Concentration-shaped, never causal (validated by the inherited causal gate)."""
    statement = (
        f"Remediation workload across {service_count} services concentrates on a "
        f"shared {common_ci_class} dependency ({workload_count} vulnerable items)."
    )
    fc.assert_not_causal(statement)
    return statement


def _build_result(h: Dict[str, Any], min_services: int) -> DetectorResult:
    material = {"org_id": h["org_id"] or "", "common_ci": h["common_ci"], "service_count": h["service_count"]}
    digest = hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:20]

    statement = _statement(h["service_count"], h["common_ci_class"], h["workload_count"])
    evidence = {
        "statement": statement,
        "common_ci_class": h["common_ci_class"],
        "service_count": h["service_count"],
        "service_ci_classes": h["service_ci_classes"],
        "workload_count": h["workload_count"],
        "max_hops": h["hops_bound"],
        "max_hop_observed": h["max_hop_observed"],
        "depth_bounded": True,
    }
    # The common CI + the contributing vulnerable items are reachable only through
    # access-controlled pointers — the CI itself is carried by an opaque record
    # pointer, never named as a host inline.
    artifacts = [{
        "type": "shared_ci",
        "id": h["common_ci"],
        "evidence_pointer": {
            "source_system": common.SOURCE_SYSTEM,
            "source_artifact": h["common_ci"],
            "source_timestamp": _cmdb_timestamp(h["workload_items"]),
            "origin": "observed",
            "source_artifact_type": "record_id",
        },
    }]
    artifacts.extend(common.pointer_artifacts(h["workload_items"], artifact_type="vulnerable_item"))

    systems = [common.SOURCE_SYSTEM, "graph"]
    confidence = fc.build_confidence(
        "MEDIUM",
        capped=True,
        eligible_for_high=False,
        cap_reason="ServiceNow VR workload + observed B3 dependency graph — single provider.",
    )
    corroboration = fc.build_corroboration(
        fc.STATUS_SINGLE_SOURCE,
        sources=systems,
        label="Workload concentrates on a shared dependency (graph-traversed, depth-bounded)",
    )
    contract = fc.build_finding_contract(
        evidence=evidence,
        confidence=confidence,
        corroboration=corroboration,
        source_trace=fc.build_source_trace(systems=systems, artifacts=artifacts),
    )
    # Belt-and-braces: the whole finding stays concentration-shaped (AC3).
    fc.assert_not_causal(statement)
    fc.assert_not_causal(corroboration["label"])

    return DetectorResult(
        detector_id=DETECTOR_ID,
        signal_source=common.SOURCE_SYSTEM,
        metric_value=float(h["service_count"]),
        threshold=float(min_services),
        raw_evidence={
            "service_count": h["service_count"],
            "workload_count": h["workload_count"],
            "common_ci_class": h["common_ci_class"],
            "max_hops": h["hops_bound"],
            "statement": statement,
            "confidence": confidence["level"],
            "corroborated": False,
            "corroboration_sources": systems,
            "finding_ref": f"servicenow:shared-infra-concentration:{digest}",
            "finding_contract": contract,
        },
    )


def _cmdb_timestamp(items) -> str:
    """A stable source timestamp for the shared-CI pointer (earliest item stamp)."""
    stamps = sorted(s for s in (common.record_timestamp(i) for i in items) if s)
    return stamps[0] if stamps else "1970-01-01T00:00:00+00:00"


def evaluate(
    sf_data: Optional[Dict[str, Any]] = None,
    sn_data: Optional[Dict[str, Any]] = None,
    jira_data: Optional[Dict[str, Any]] = None,
):
    hotspots = _hotspots(sn_data)
    t = _thresholds()
    top = hotspots[0] if hotspots else None
    return make_detector_evaluation(
        module_name=__name__,
        detector_id=DETECTOR_ID,
        signal_source=common.SOURCE_SYSTEM,
        metric_value=float(top["service_count"]) if top else 0.0,
        threshold=float(t.get("min_services", DEFAULT_MIN_SERVICES)),
        fired=bool(hotspots),
        raw_evidence={
            "service_count": top["service_count"] if top else 0,
            "workload_count": top["workload_count"] if top else 0,
            "max_hops": int(t.get("max_hops", DEFAULT_MAX_HOPS)),
            "hotspot_count": len(hotspots),
        },
    )


def detect(
    sf_data: Optional[Dict[str, Any]] = None,
    sn_data: Optional[Dict[str, Any]] = None,
    jira_data: Optional[Dict[str, Any]] = None,
) -> List[DetectorResult]:
    t = _thresholds()
    min_services = int(t.get("min_services", DEFAULT_MIN_SERVICES) or DEFAULT_MIN_SERVICES)
    return [_build_result(h, min_services) for h in _hotspots(sn_data)]
