"""
ci_dependency_graph.py — the ONE depth-bounded CI-dependency traversal engine.

Every detector that walks MSP-B3 dependency edges walks THEM THROUGH HERE. Before
this module the same walk existed three times — ``cloud_ops_shared_ci_hotspot``
built its own adjacency and its own BFS, ``security_ops_common.cmdb_adjacency``
built a second adjacency, and ``security_ops_shared_infra_concentration`` carried a
byte-identical copy of the BFS. Three copies of one rule is three chances for the
rule to drift, and they HAD drifted: only the SecOps adjacency normalised edge
DIRECTION (``used_by``/``hosts``/``runs`` point the other way), so the same CMDB fed
to the two packs could produce different reachability and therefore different
findings. Consolidating them is the "share the extraction" discipline the R17-A3/A4
operational modules and the MSP-B1 cloud skeleton already apply.

What lives here
---------------
* :func:`build_adjacency` — raw B3 edges → a directed ``adj[X] = [CIs X depends on]``
  graph. Tolerant of every edge shape this codebase's producers emit (the SecOps
  ``relationships`` records keyed ``source_ci_id``/``from_ci_sys_id``/``parent``, and
  the cloud-ops ``ci_graph.edges`` keyed ``from``/``to``/``source``/``target``), and
  direction-normalising via ``relationship_type`` so an edge always points from the
  dependent CI to the more underlying one.
* :func:`reachable_within` — the depth-bounded BFS: ``{ci: shortest_hop}`` for hops
  ``1..max_hops``, EXCLUDING the start CI (the concentration target is a downstream
  shared dependency, never the origin's own CI). A CI reachable only beyond the
  bound does not count — the property MSP-B6 T3-AC2 and MSP-B12 both rest on.

Why NOT ``app.graph_context_builder``
-------------------------------------
That module is a different tool for a different job: it reads the PERSISTED graph
through ``app.graph_query`` (a DB round-trip), is seeded by an OPPORTUNITY, and — by
design — RANKS and HARD-CAPS its output to 15 entities / 20 relationships so an LLM
prompt stays bounded. A detector needs the opposite: a pure, offline, uncapped walk
over the normalised signal block it was handed, where dropping an edge silently
changes whether a finding fires. Routing detectors through the prompt builder would
add a DB dependency to offline detection and let a cap decide a detector's verdict.
The duplication worth removing is between the detectors themselves — which is what
this module removes.

Pure and deterministic: no DB, no ``app`` import beyond none at all, no clock.
Neighbour lists are sorted, so traversal order (and therefore every hop number it
produces) is stable for a given edge set.
"""
from __future__ import annotations

from collections import deque
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

#: Relationship types where the SOURCE depends on the TARGET (the target is the more
#: underlying CI). An edge carrying no relationship_type is treated as depends-on,
#: which is what the plain ``{from, to}`` cloud-ops edge shape means.
DEPENDS_FORWARD = frozenset({"depends_on", "runs_on", "uses", "connects_to", "hosted_on"})

#: The inverse spellings — the TARGET depends on the SOURCE, so the edge is reversed
#: before it enters the adjacency.
DEPENDS_REVERSE = frozenset({"used_by", "hosts", "runs"})

#: Edge key aliases, most-specific first. Kept in one place so a new producer shape
#: is added once rather than in each detector.
_SOURCE_KEYS: Tuple[str, ...] = (
    "source_ci_id", "from_ci_sys_id", "from_ci", "parent", "from", "source",
)
_TARGET_KEYS: Tuple[str, ...] = (
    "target_ci_id", "to_ci_sys_id", "to_ci", "child", "to", "target",
)


def _scalar(value: Any) -> Optional[str]:
    """Extract a CI identifier scalar, tolerating ServiceNow's nested value shapes.

    A ServiceNow read with ``sysparm_display_value=all`` returns each reference field
    as a mapping; the sys_id/value member is the stable identifier to key a graph on.
    """
    if isinstance(value, Mapping):
        value = (
            value.get("sys_id")
            or value.get("value")
            or value.get("display_value")
            or value.get("name")
        )
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _first(edge: Mapping[str, Any], keys: Sequence[str]) -> Optional[str]:
    for key in keys:
        if key in edge:
            found = _scalar(edge.get(key))
            if found:
                return found
    return None


def build_adjacency(edges: Optional[Iterable[Any]]) -> Dict[str, List[str]]:
    """Build the directed depends-on graph: ``adj[X]`` = the CIs ``X`` depends on.

    ``edges`` is any iterable of edge mappings. Direction is normalised so an edge
    always points from the dependent CI to the underlying CI (a ``used_by``/``hosts``
    /``runs`` edge is reversed); an edge missing either endpoint is skipped rather
    than half-added. Neighbour lists are de-duplicated and sorted, which is what
    makes :func:`reachable_within` deterministic.
    """
    adj: Dict[str, List[str]] = {}
    for edge in edges or ():
        if not isinstance(edge, Mapping):
            continue
        src = _first(edge, _SOURCE_KEYS)
        dst = _first(edge, _TARGET_KEYS)
        if not src or not dst:
            continue
        rel = str(edge.get("relationship_type") or edge.get("type") or "depends_on").strip().lower()
        if rel in DEPENDS_REVERSE:
            src, dst = dst, src
        adj.setdefault(src, [])
        if dst not in adj[src]:
            adj[src].append(dst)
    for node in adj:
        adj[node].sort()
    return adj


def reachable_within(
    adj: Mapping[str, Sequence[str]], start: str, max_hops: int
) -> Dict[str, int]:
    """BFS from ``start`` along depends-on edges → ``{ci: shortest_hop}`` for 1..``max_hops``.

    ``start`` itself is EXCLUDED: the concentration target is a downstream shared
    DEPENDENCY, not the origin's own CI. A CI reachable only beyond ``max_hops`` is
    absent from the result — the depth bound is the guarantee, not a hint. Cycles
    terminate (each CI is visited once, at its shortest hop). ``max_hops <= 0``
    reaches nothing.
    """
    reached: Dict[str, int] = {}
    if not start or max_hops <= 0:
        return reached
    queue: deque[Tuple[str, int]] = deque([(start, 0)])
    seen = {start}
    while queue:
        node, hops = queue.popleft()
        if hops >= max_hops:
            continue
        for nxt in adj.get(node, ()) or ():
            if nxt in seen:
                continue
            seen.add(nxt)
            reached[nxt] = hops + 1
            queue.append((nxt, hops + 1))
    return reached


__all__ = [
    "DEPENDS_FORWARD",
    "DEPENDS_REVERSE",
    "build_adjacency",
    "reachable_within",
]
