"""
R17-A2 / AT-460 (T3) — Confluence reach-phase signal extraction.

Turns the structured Confluence content records produced by
:mod:`discovery.ingest.confluence` (AT-457) into the operational SIGNAL types the
story's Section 2 calls for — WITHOUT reading the page body. This is the reach
phase: counts, timing, contributor patterns, and pattern-matched markers over
metadata only. Deep page-content NLP is the separate 1.8 story and is deliberately
NOT done here (AC7).

The signal types (Section 2, Confluence row)
--------------------------------------------
1. **Space & page activity** — what pages exist (inventory), update cadence
   (pages updated per day), churn (edit accumulation via ``version.number``), and
   contributor patterns (distinct editors). Per space, whether it is ACTIVE
   (recent edits) or dormant. See :func:`extract_space_activity`.
2. **Cross-reference markers** — mentions of tickets / PRs / systems in the page
   **title** (metadata) that let corroboration link a Confluence signal to a
   finding elsewhere. Structured pattern matching over ids/URLs, NOT content
   understanding — the source-agnostic extractor from
   :mod:`discovery.ingest.slack_signals` is reused verbatim.
3. **Stale-but-load-bearing pages** — pages that are rarely updated (stale) yet
   load-bearing. True "frequently linked" requires the inbound-link graph, which
   is a 1.8 content-layer signal; in the reach phase we approximate load-bearing
   with a metadata-only proxy — a high accumulated edit count (``version.number``)
   marks a page that has been actively maintained historically. A stale page with
   a high version count is the early "important but neglected" signal.

Why title-marker matching is not "deep content" (AC7)
-----------------------------------------------------
Extracting ``INC-4821`` from a page *title* is the same structured-id operation
the Slack/Teams connectors already do — it reads a metadata field, never the page
body. The page body (``body.storage``) is never fetched by the ingestor, so it is
not available here and is never read.

Determinism
-----------
"Stale" and "active/dormant" are computed relative to the corpus's most-recent
edit (the max ``modified_at`` across the records), NOT the wall clock — so the
signal is reproducible from the same data.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

# Cross-reference marker extraction is source-agnostic structured pattern matching
# (ServiceNow/Jira/GitHub ids + URLs). Reuse the Slack implementation so a
# ticket/PR reference is detected identically across every source.
from .slack_signals import extract_cross_reference_markers

__all__ = [
    "extract_cross_reference_markers",
    "build_page_signals",
    "extract_space_activity",
    "build_confluence_signal",
    "build_confluence_corroboration_payload",
    "CONFLUENCE_CORROBORATION_KEY",
    "STALE_WINDOW_DAYS",
    "LOAD_BEARING_MIN_VERSIONS",
    "ACTIVE_WINDOW_DAYS",
]

#: A page not edited within this many days of the corpus's most-recent edit is
#: "stale" (rarely updated).
STALE_WINDOW_DAYS = 90
#: A page edited at least this many times (``version.number``) is treated as
#: load-bearing — a metadata-only proxy for "important / frequently maintained",
#: since true inbound-link counts are a 1.8 content-graph signal.
LOAD_BEARING_MIN_VERSIONS = 5
#: A space with an edit within this many days of the corpus's most-recent edit is
#: ACTIVE; otherwise it is dormant.
ACTIVE_WINDOW_DAYS = 30
_SECONDS_PER_DAY = 86400.0


def _iso_to_epoch(value: Optional[str]) -> Optional[float]:
    """Parse an ISO-8601 timestamp (Confluence uses ``...Z``) to epoch seconds, or None."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def _version_number(record: Dict[str, Any]) -> int:
    try:
        return int(record.get("version_number", 1) or 1)
    except (TypeError, ValueError):
        return 1


def build_page_signals(record: Dict[str, Any]) -> Dict[str, Any]:
    """Per-page reach-phase signal block attached to each delta record.

    Contains the cross-reference markers found in the page **title** (metadata)
    and the page's edit-activity (version count + high-churn flag). No body is
    read (AC7).
    """
    version = _version_number(record)
    return {
        "cross_references": extract_cross_reference_markers(record.get("title", "")),
        "activity": {
            "version_number": version,
            "high_churn": version >= LOAD_BEARING_MIN_VERSIONS,
        },
    }


def extract_space_activity(
    space_key: str, space_name: str, records: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """Extract inventory / cadence / churn / contributor signal for one space.

    Reads only metadata fields (``modified_at``, ``modified_by``,
    ``version_number``, ``content_type``). ``is_active`` is filled in by
    :func:`build_confluence_signal`, which knows the corpus-wide "as of" moment.
    """
    epochs: List[float] = []
    contributors: set = set()
    versions: List[int] = []
    page_count = 0
    blogpost_count = 0
    for r in records:
        ep = _iso_to_epoch(r.get("modified_at"))
        if ep is not None:
            epochs.append(ep)
        if r.get("modified_by"):
            contributors.add(r["modified_by"])
        versions.append(_version_number(r))
        if r.get("content_type") == "blogpost":
            blogpost_count += 1
        else:
            page_count += 1

    epochs.sort()
    if epochs:
        span_seconds = epochs[-1] - epochs[0]
        first_epoch, last_epoch = epochs[0], epochs[-1]
    else:
        span_seconds = 0.0
        first_epoch = last_epoch = None

    days = max(span_seconds / _SECONDS_PER_DAY, 1.0)
    cadence_per_day = round(len(records) / days, 4)
    churn_avg = round(sum(versions) / len(versions), 4) if versions else 0.0

    def _iso(epoch: Optional[float]) -> Optional[str]:
        if epoch is None:
            return None
        return datetime.utcfromtimestamp(epoch).isoformat() + "Z"

    return {
        "space_key": space_key,
        "space_name": space_name,
        "content_count": len(records),
        "page_count": page_count,
        "blogpost_count": blogpost_count,
        "contributor_count": len(contributors),
        "span_seconds": round(span_seconds, 3),
        "cadence_per_day": cadence_per_day,
        "churn_avg": churn_avg,
        "first_modified": _iso(first_epoch),
        "last_modified": _iso(last_epoch),
        "_last_epoch": last_epoch,  # internal — stripped before returning
    }


def build_confluence_signal(records: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Aggregate Confluence delta records into the downstream signal block.

    Produces per-space activity (with ACTIVE/dormant classification), the
    de-duplicated set of cross-reference markers, and the stale-but-load-bearing
    page list. All derived from metadata — no page body is read (AC7).
    """
    records = list(records)

    # Corpus "as of" = the most-recent edit across all records (deterministic;
    # not the wall clock), used for stale / active-dormant windows.
    as_of = max(
        (e for e in (_iso_to_epoch(r.get("modified_at")) for r in records) if e is not None),
        default=None,
    )

    by_space: Dict[str, List[Dict[str, Any]]] = {}
    space_names: Dict[str, str] = {}
    for r in records:
        key = r.get("space_key", "")
        by_space.setdefault(key, []).append(r)
        space_names.setdefault(key, r.get("space_name", ""))

    activity: Dict[str, Dict[str, Any]] = {}
    for key, recs in by_space.items():
        a = extract_space_activity(key, space_names.get(key, ""), recs)
        last_epoch = a.pop("_last_epoch", None)
        if as_of is not None and last_epoch is not None:
            a["is_active"] = (as_of - last_epoch) <= ACTIVE_WINDOW_DAYS * _SECONDS_PER_DAY
        else:
            a["is_active"] = False
        activity[key] = a

    # Cross-reference markers (from titles), de-duplicated across all records.
    cross_references: List[Dict[str, str]] = []
    seen: set = set()
    for r in records:
        for mk in extract_cross_reference_markers(r.get("title", "")):
            dedup_key = (mk.get("system"), (mk.get("ref") or "").upper())
            if dedup_key not in seen:
                seen.add(dedup_key)
                cross_references.append(mk)

    # Stale-but-load-bearing: rarely updated (stale relative to as_of) AND a high
    # accumulated edit count (metadata proxy for load-bearing).
    stale_load_bearing: List[Dict[str, Any]] = []
    if as_of is not None:
        stale_cutoff = as_of - STALE_WINDOW_DAYS * _SECONDS_PER_DAY
        for r in records:
            ep = _iso_to_epoch(r.get("modified_at"))
            version = _version_number(r)
            if ep is not None and ep < stale_cutoff and version >= LOAD_BEARING_MIN_VERSIONS:
                stale_load_bearing.append(
                    {
                        "artifact_id": r.get("artifact_id"),
                        "space_key": r.get("space_key"),
                        "title": r.get("title"),
                        "version_number": version,
                        "modified_at": r.get("modified_at"),
                    }
                )

    return {
        "activity": activity,
        "cross_references": cross_references,
        "stale_load_bearing": stale_load_bearing,
    }


#: The connector's source identity in the corroboration input. Confluence is a
#: knowledge/document source; the block MUST be fed under this key and reported as
#: Confluence so downstream corroboration rules apply to it correctly.
CONFLUENCE_CORROBORATION_KEY = "confluence"


def build_confluence_corroboration_payload(
    records: Iterable[Dict[str, Any]],
) -> Dict[str, Any]:
    """Package Confluence signal into the corroboration-engine input block.

    Wraps :func:`build_confluence_signal` under the ``'confluence'`` key. This
    only *produces* the signal in the shape downstream consumers read — it
    attaches no confidence and performs no elevation.
    """
    return {CONFLUENCE_CORROBORATION_KEY: build_confluence_signal(records)}
