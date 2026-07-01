"""
R17-A2 / AT-460 (T3) — SharePoint reach-phase signal extraction.

Turns the structured SharePoint driveItem records produced by
:mod:`discovery.ingest.sharepoint` (AT-459) into the operational SIGNAL types the
story's Section 2 calls for — WITHOUT reading any document body. This is the reach
phase: counts, structure, timing, ownership, and pattern-matched markers over
metadata only. Deep document-content NLP is the separate 1.8 story and is
deliberately NOT done here (AC7).

The signal types (Section 2, SharePoint row)
--------------------------------------------
1. **Document-library activity** — document & folder structure (file vs folder
   counts, folder depth), modification cadence (items changed per day), and
   ownership patterns (distinct creators/editors). Per library, whether the
   estate is ACTIVE or DORMANT. See :func:`extract_library_activity`.
2. **Cross-reference markers** — mentions of tickets / PRs / systems in the
   **item name** (metadata, e.g. ``Q3-incident-INC-4821.docx``) that let
   corroboration link a SharePoint signal to a finding elsewhere. Structured
   pattern matching over ids/URLs, reusing the source-agnostic extractor.
3. **Active vs dormant estates** — which document libraries are actively touched
   versus dormant, derived from the most-recent modification per library.

Why item-name marker matching is not "deep content" (AC7)
---------------------------------------------------------
Extracting ``INC-4821`` from a *file name* reads a metadata field, never the
document body. The driveItem body/content is never fetched by the ingestor, so it
is not available here and is never read.

Determinism
-----------
"Active/dormant" is computed relative to the corpus's most-recent modification
(the max item timestamp across the records), NOT the wall clock — so the signal
is reproducible from the same data.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

# Source-agnostic structured id/URL extraction — reused so a ticket/PR reference
# is detected identically across every source.
from .slack_signals import extract_cross_reference_markers

__all__ = [
    "extract_cross_reference_markers",
    "build_item_signals",
    "extract_library_activity",
    "build_sharepoint_signal",
    "build_sharepoint_corroboration_payload",
    "SHAREPOINT_CORROBORATION_KEY",
    "ACTIVE_WINDOW_DAYS",
]

#: A library with a modification within this many days of the corpus's most-recent
#: modification is ACTIVE; otherwise the estate is DORMANT.
ACTIVE_WINDOW_DAYS = 30
_SECONDS_PER_DAY = 86400.0


def _iso_to_epoch(value: Optional[str]) -> Optional[float]:
    """Parse an ISO-8601 timestamp (Graph uses ``...Z``) to epoch seconds, or None."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def _item_timestamp(record: Dict[str, Any]) -> Optional[str]:
    """The item's own change moment: last-modified if edited, else created."""
    return record.get("last_modified_at") or record.get("created_at")


def _folder_depth(parent_path: Optional[str]) -> int:
    """Folder depth from a driveItem ``parentReference.path``.

    Graph paths look like ``/drive/root:`` (depth 0) or ``/drive/root:/A/B``
    (depth 2). Everything after ``root:`` is the folder chain.
    """
    if not parent_path or "root:" not in parent_path:
        return 0
    tail = parent_path.split("root:", 1)[1].strip("/")
    return len([seg for seg in tail.split("/") if seg]) if tail else 0


def build_item_signals(record: Dict[str, Any]) -> Dict[str, Any]:
    """Per-item reach-phase signal block attached to each delta record.

    Contains the cross-reference markers found in the **item name** (metadata) and
    the item's structural metadata (kind, size, folder depth). No body is read
    (AC7).
    """
    return {
        "cross_references": extract_cross_reference_markers(record.get("item_name", "")),
        "activity": {
            "item_type": record.get("item_type"),
            "size": record.get("size"),
            "depth": _folder_depth(record.get("parent_path")),
        },
    }


def extract_library_activity(
    library_key: str,
    site_name: str,
    library_name: str,
    records: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Extract structure / cadence / ownership signal for one document library.

    Reads only metadata (item type, timestamps, creators/editors, parent path).
    ``is_active`` is filled in by :func:`build_sharepoint_signal`, which knows the
    corpus-wide "as of" moment.
    """
    epochs: List[float] = []
    owners: set = set()
    editors: set = set()
    file_count = 0
    folder_count = 0
    max_depth = 0
    for r in records:
        ep = _iso_to_epoch(_item_timestamp(r))
        if ep is not None:
            epochs.append(ep)
        if r.get("created_by"):
            owners.add(r["created_by"])
        if r.get("last_modified_by"):
            editors.add(r["last_modified_by"])
        if r.get("item_type") == "folder":
            folder_count += 1
        elif r.get("item_type") == "file":
            file_count += 1
        max_depth = max(max_depth, _folder_depth(r.get("parent_path")))

    epochs.sort()
    if epochs:
        span_seconds = epochs[-1] - epochs[0]
        first_epoch, last_epoch = epochs[0], epochs[-1]
    else:
        span_seconds = 0.0
        first_epoch = last_epoch = None

    days = max(span_seconds / _SECONDS_PER_DAY, 1.0)
    cadence_per_day = round(len(records) / days, 4)

    def _iso(epoch: Optional[float]) -> Optional[str]:
        if epoch is None:
            return None
        return datetime.utcfromtimestamp(epoch).isoformat() + "Z"

    return {
        "library_key": library_key,
        "site_name": site_name,
        "library_name": library_name,
        "item_count": len(records),
        "file_count": file_count,
        "folder_count": folder_count,
        "owner_count": len(owners),
        "editor_count": len(editors),
        "max_depth": max_depth,
        "span_seconds": round(span_seconds, 3),
        "cadence_per_day": cadence_per_day,
        "first_modified": _iso(first_epoch),
        "last_modified": _iso(last_epoch),
        "_last_epoch": last_epoch,  # internal — stripped before returning
    }


def build_sharepoint_signal(records: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Aggregate SharePoint delta records into the downstream signal block.

    Produces per-library activity (with ACTIVE/DORMANT classification), the
    de-duplicated set of cross-reference markers (from item names), and the
    active/dormant estate split. All derived from metadata — no document body is
    read (AC7).
    """
    records = list(records)

    as_of = max(
        (e for e in (_iso_to_epoch(_item_timestamp(r)) for r in records) if e is not None),
        default=None,
    )

    by_lib: Dict[str, List[Dict[str, Any]]] = {}
    lib_meta: Dict[str, Dict[str, str]] = {}
    for r in records:
        key = f"{r.get('site_id', '')}/{r.get('drive_id', '')}"
        by_lib.setdefault(key, []).append(r)
        lib_meta.setdefault(
            key,
            {"site_name": r.get("site_name", ""), "library_name": r.get("library_name", "")},
        )

    activity: Dict[str, Dict[str, Any]] = {}
    active: List[str] = []
    dormant: List[str] = []
    for key, recs in by_lib.items():
        meta = lib_meta.get(key, {})
        a = extract_library_activity(
            key, meta.get("site_name", ""), meta.get("library_name", ""), recs
        )
        last_epoch = a.pop("_last_epoch", None)
        is_active = (
            as_of is not None
            and last_epoch is not None
            and (as_of - last_epoch) <= ACTIVE_WINDOW_DAYS * _SECONDS_PER_DAY
        )
        a["is_active"] = is_active
        activity[key] = a
        (active if is_active else dormant).append(key)

    cross_references: List[Dict[str, str]] = []
    seen: set = set()
    for r in records:
        for mk in extract_cross_reference_markers(r.get("item_name", "")):
            dedup_key = (mk.get("system"), (mk.get("ref") or "").upper())
            if dedup_key not in seen:
                seen.add(dedup_key)
                cross_references.append(mk)

    return {
        "activity": activity,
        "cross_references": cross_references,
        "estates": {"active": sorted(active), "dormant": sorted(dormant)},
    }


#: The connector's source identity in the corroboration input.
SHAREPOINT_CORROBORATION_KEY = "sharepoint"


def build_sharepoint_corroboration_payload(
    records: Iterable[Dict[str, Any]],
) -> Dict[str, Any]:
    """Package SharePoint signal into the corroboration-engine input block.

    Wraps :func:`build_sharepoint_signal` under the ``'sharepoint'`` key. Only
    *produces* the signal in the shape downstream consumers read — attaches no
    confidence and performs no elevation.
    """
    return {SHAREPOINT_CORROBORATION_KEY: build_sharepoint_signal(records)}
