"""
Sprint 4 T3 — Cross-System Linker

Scans evidence objects for known ticket reference patterns (INC-, CS-, JIRA-)
and groups them into deterministic LinkedCluster[] objects per run.

Changes from v1.0:
  - Fix 3: _ts_to_epoch() parses tsLabel string as fallback when tsEpoch is
    absent. Track B evidence objects carry tsLabel (formatted string) not
    tsEpoch. Without this, tsEpochMax is always 0 and sort falls back to
    alphabetical only.
"""
import re
from datetime import datetime
from typing import Dict, List, Any, Tuple

from .models_clusters import LinkedCluster

_PATTERNS: List[Tuple[str, re.Pattern]] = [
    ("INC",  re.compile(r"\bINC-\d{3,}\b",  re.IGNORECASE)),
    ("CS",   re.compile(r"\bCS-\d{3,}\b",   re.IGNORECASE)),
    ("JIRA", re.compile(r"\bJIRA-\d{1,}\b", re.IGNORECASE)),
]

# tsLabel format produced by Track B evidence_builder.py
_TS_LABEL_FMT = "%d %b %Y, %H:%M"


def _ts_to_epoch(ev: Dict[str, Any]) -> int:
    """
    Return a Unix epoch integer for an evidence object.

    Track B evidence objects carry tsLabel (e.g. "17 Apr 2026, 14:23") but
    not tsEpoch. This function tries tsEpoch first for forward-compatibility,
    then parses tsLabel as a fallback.
    """
    raw = ev.get("tsEpoch")
    if raw:
        try:
            return int(raw)
        except (TypeError, ValueError):
            pass
    ts_label = ev.get("tsLabel", "")
    if ts_label:
        try:
            return int(datetime.strptime(ts_label, _TS_LABEL_FMT).timestamp())
        except (ValueError, OSError):
            pass
    return 0


def _extract_keys(text: str) -> List[str]:
    """Extract and deduplicate ticket reference keys from a text string."""
    if not text:
        return []
    keys: List[str] = []
    for _, pat in _PATTERNS:
        for m in pat.findall(text):
            keys.append(m.upper())
    seen: set = set()
    out: List[str] = []
    for k in keys:
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


def build_clusters(evidence: List[Dict[str, Any]]) -> List[LinkedCluster]:
    """
    Group evidence objects by shared ticket reference keys.

    Determinism guarantees:
      - evidenceIds within each bucket are sorted alphabetically.
      - sources within each bucket are sorted alphabetically.
      - Clusters are sorted by (-tsEpochMax, key, -len(evidenceIds)).
      - Cluster IDs (clu_001 ...) are assigned after sorting, so the same
        evidence always produces the same IDs.

    Returns an empty list if no cross-system references are detected.
    """
    buckets: Dict[str, Dict[str, Any]] = {}

    for ev in evidence or []:
        ev_id = ev.get("id")
        src   = ev.get("source") or "Unknown"
        text  = " ".join([
            str(ev.get("title")   or ""),
            str(ev.get("snippet") or ""),
        ])
        keys     = _extract_keys(text)
        ts_epoch = _ts_to_epoch(ev)

        for k in keys:
            b = buckets.setdefault(k, {
                "evidenceIds": [],
                "sources":     set(),
                "tsEpochMax":  0,
            })
            if ev_id and ev_id not in b["evidenceIds"]:
                b["evidenceIds"].append(ev_id)
            b["sources"].add(src)
            if ts_epoch > b["tsEpochMax"]:
                b["tsEpochMax"] = ts_epoch

    # Sort for determinism before assigning positional IDs
    items = []
    for key, b in buckets.items():
        sources  = sorted(list(b["sources"]))
        ev_ids   = sorted(b["evidenceIds"])
        ts_max   = int(b["tsEpochMax"] or 0)
        items.append((key, sources, ev_ids, ts_max))

    items.sort(key=lambda t: (-t[3], t[0], -len(t[2])))

    clusters: List[LinkedCluster] = []
    for i, (key, sources, ev_ids, ts_max) in enumerate(items, start=1):
        clusters.append(LinkedCluster(
            id=f"clu_{i:03d}",
            key=key,
            normalizedKey=key,
            sources=sources,
            evidenceIds=ev_ids,
            summary=f"Reference {key} appears across {len(sources)} system(s)",
            tsEpochMax=ts_max,
        ))
    return clusters
