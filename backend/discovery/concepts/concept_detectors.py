"""2.0-B4 T4 (AT-813) — concept-only detectors that run across source families.

AC3: *a detector written only against normalised concepts runs across at least three
different source families without modification.*

`detect_open_work_item_backlog` is such a detector. It reads ONLY the normalised
`WorkItem` concept — `is_open` (derived from the coarse `status_category`) and the
group reference on `assigned_group` — and knows nothing about any connector. It is not
a port of an existing detector (that was T3); it is written concept-first. Fed
`WorkItem`s mapped from ServiceNow, Jira, Salesforce and GitHub (four source families;
AC3 needs three), the SAME function produces a backlog finding per family, unchanged.

It never branches on a source's identity. `source_system` is used only as a grouping
DIMENSION (so a mixed multi-family stream yields one finding per family) — there is no
`if source_system == "jira"` anywhere, which a structural test in
`tests/unit/test_r2_0_b4_t4_cross_family_run.py` pins.

AC5 shows through the same detector honestly. A family that declares an `actor_group`
gap (Jira, GitHub) maps its work items with `assigned_group = None`, so the finding
reports those items under `ungrouped_count` rather than inventing a group from an
individual's name — the gap is visible in the output, never silently approximated.
"""

from __future__ import annotations

from typing import Dict, Iterable, List

try:
    from discovery.concepts.model import WorkItem
    from discovery.models import DetectorResult
except ModuleNotFoundError:  # project-root execution uses backend as package
    from backend.discovery.concepts.model import WorkItem
    from backend.discovery.models import DetectorResult

DETECTOR_ID = "OPEN_WORK_ITEM_BACKLOG"

#: Minimum open work items for a source's backlog to fire. A plain calibration
#: constant — the detector carries no per-family knowledge of any kind.
DEFAULT_MIN_OPEN = 3


def _group_label(work_item: WorkItem) -> str | None:
    """The group a work item is assigned to, or None when the source assigns to an
    individual (an ``actor_group`` gap) — in which case a group is NEVER fabricated."""
    ref = work_item.assigned_group
    if ref is None:
        return None
    return ref.display_name or ref.source_record_id or None


def detect_open_work_item_backlog(
    signals: Iterable[object], *, min_open: int = DEFAULT_MIN_OPEN
) -> List[DetectorResult]:
    """Fire, per source family present, when its OPEN work-item backlog reaches
    ``min_open``. Reads only the ``WorkItem`` concept — runs unchanged across every
    family that supplies work items.

    One finding per ``source_system`` with a qualifying backlog, in deterministic
    (sorted) source order. Each finding's evidence carries the open count, the
    per-group breakdown, and the count of items with no group (the honest surface of
    an ``actor_group`` gap).
    """
    open_by_source: Dict[str, List[WorkItem]] = {}
    for signal in signals:
        if not isinstance(signal, WorkItem):
            continue
        if signal.is_open:
            open_by_source.setdefault(signal.source_system, []).append(signal)

    results: List[DetectorResult] = []
    for source_system in sorted(open_by_source):
        items = open_by_source[source_system]
        open_count = len(items)
        if open_count < min_open:
            continue

        groups: Dict[str, int] = {}
        ungrouped = 0
        for work_item in items:
            label = _group_label(work_item)
            if label is None:
                ungrouped += 1
            else:
                groups[label] = groups.get(label, 0) + 1

        results.append(
            DetectorResult(
                detector_id=DETECTOR_ID,
                signal_source=source_system,
                metric_value=float(open_count),
                threshold=float(min_open),
                raw_evidence={
                    "source_system": source_system,
                    "open_count": open_count,
                    "ungrouped_count": ungrouped,
                    "groups": dict(sorted(groups.items())),
                },
            )
        )

    return results


__all__ = ["DETECTOR_ID", "DEFAULT_MIN_OPEN", "detect_open_work_item_backlog"]
