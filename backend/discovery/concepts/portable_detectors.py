"""2.0-B4 T3 — concept-native detector ports (AT-812 / AC2).

Two existing detectors, re-expressed to read ONLY the normalised concept set, and
proven (in ``tests/unit/test_r2_0_b4_t3_detector_portability.py``) to produce
findings identical to their originals on golden fixtures:

* :func:`detect_approval_bottleneck` ← ``discovery.detectors.approval_delay``
  (``APPROVAL_BOTTLENECK``) — reads :class:`~discovery.concepts.model.Approval`.
* :func:`detect_permission_bottleneck` ←
  ``discovery.detectors.permission_bottleneck`` (``PERMISSION_BOTTLENECK``) — reads
  :class:`Approval` plus the :class:`~discovery.concepts.model.ActorGroup` its
  ``approver_group`` points at.

What changed, and what deliberately did not. The ORIGINAL reads a connector-specific
shape — ``sf_data['approval_processes'][i]['avg_delay_days']`` — and is bound, by the
field names it knows, to Salesforce. The PORT reads a concept stream: a flat list of
``ConceptSignal`` it filters to ``Approval`` gates and their ``ActorGroup`` approver
groups. It names no source and no source field path. The calibration is *identical* —
the threshold constants are imported from the original modules, not re-declared, so
the proof's claim ("same logic; only the input is normalised") is literally true and
cannot drift: change a threshold in the original and the port changes with it.

Why this matters. A detector written this way is the thing 2.0-B4 exists to enable
(and 2.0-C3's SDK depends on): the same logic runs wherever the concept exists,
without a per-connector copy. AC2 proves the port is behaviour-preserving on one
family; AC3 (a separate ticket) then runs a concept-native detector across three.

Where the discriminating numbers live. ``approver_count`` is read from the normalised
``ActorGroup.member_count`` — a first-class concept aggregate, not a connector field.
The source's own pre-computed scores (``avg_delay_days``, ``bottleneck_score``,
``pending_count``) are read from the ``Approval.attributes`` bag, which is where the
mapper places source-computed measurements (B0's ``payload`` rule). The port never
reaches into a connector dict for any of them.
"""

from __future__ import annotations

from typing import Iterable, List

try:
    from discovery.concepts.mappers import actor_group_key, approver_group_ref_key
    from discovery.concepts.model import ActorGroup, Approval, ConceptSignal
    from discovery.detectors.approval_delay import (
        BOTTLENECK_THRESHOLD,
        DELAY_THRESHOLD,
        DETECTOR_ID as APPROVAL_BOTTLENECK_ID,
        SEVERE_DELAY,
    )
    from discovery.detectors.permission_bottleneck import (
        DETECTOR_ID as PERMISSION_BOTTLENECK_ID,
        THRESHOLD as PERMISSION_THRESHOLD,
    )
    from discovery.models import DetectorResult
except ModuleNotFoundError:  # project-root execution uses backend as package
    from backend.discovery.concepts.mappers import (
        actor_group_key,
        approver_group_ref_key,
    )
    from backend.discovery.concepts.model import ActorGroup, Approval, ConceptSignal
    from backend.discovery.detectors.approval_delay import (
        BOTTLENECK_THRESHOLD,
        DELAY_THRESHOLD,
        DETECTOR_ID as APPROVAL_BOTTLENECK_ID,
        SEVERE_DELAY,
    )
    from backend.discovery.detectors.permission_bottleneck import (
        DETECTOR_ID as PERMISSION_BOTTLENECK_ID,
        THRESHOLD as PERMISSION_THRESHOLD,
    )
    from backend.discovery.models import DetectorResult

_SIGNAL_SOURCE = "salesforce"


def _approver_count_for(approval: Approval, groups: dict) -> int:
    """Resolve an ``Approval``'s approver count from the normalised
    ``ActorGroup.member_count`` its ``approver_group`` reference points at.

    A reference with no matching group (or a group with no member count) reads 0 —
    the same value the original detector gets from a missing ``approver_count`` — so
    the port degrades exactly as the original does rather than raising."""
    group = groups.get(approver_group_ref_key(approval))
    if group is None or group.member_count is None:
        return 0
    return int(group.member_count)


def _index_groups(signals: Iterable[ConceptSignal]) -> dict:
    return {
        actor_group_key(s): s for s in signals if isinstance(s, ActorGroup)
    }


def detect_approval_bottleneck(signals: Iterable[ConceptSignal]) -> List[DetectorResult]:
    """Concept-native ``APPROVAL_BOTTLENECK`` — fires per approval gate on delay.

    Behaviour-identical to ``discovery.detectors.approval_delay.detect``: fires when
    ``(avg_delay_days > DELAY_THRESHOLD and bottleneck_score > BOTTLENECK_THRESHOLD)``
    or ``avg_delay_days > SEVERE_DELAY``, emitting one finding per firing gate in
    stream order.
    """
    signals = list(signals)
    groups = _index_groups(signals)
    results: List[DetectorResult] = []

    for approval in signals:
        if not isinstance(approval, Approval):
            continue
        attrs = approval.attributes or {}
        delay = float(attrs.get("avg_delay_days", 0.0))
        b_score = float(attrs.get("bottleneck_score", 0.0))
        pending = int(attrs.get("pending_count", 0))
        approver_count = _approver_count_for(approval, groups)

        combined_fires = delay > DELAY_THRESHOLD and b_score > BOTTLENECK_THRESHOLD
        severe_fires = delay > SEVERE_DELAY
        if not (combined_fires or severe_fires):
            continue

        results.append(
            DetectorResult(
                detector_id=APPROVAL_BOTTLENECK_ID,
                signal_source=_SIGNAL_SOURCE,
                metric_value=round(delay, 2),
                threshold=DELAY_THRESHOLD,
                raw_evidence={
                    "process_name": str(attrs.get("process_name", "")),
                    "pending_count": pending,
                    "avg_delay_days": delay,
                    "approver_count": approver_count,
                    "bottleneck_score": b_score,
                },
            )
        )

    return results


def detect_permission_bottleneck(
    signals: Iterable[ConceptSignal],
) -> List[DetectorResult]:
    """Concept-native ``PERMISSION_BOTTLENECK`` — fires per gate on approver
    concentration.

    Behaviour-identical to ``discovery.detectors.permission_bottleneck.detect``:
    fires when ``approver_count > 0 and bottleneck_score > PERMISSION_THRESHOLD``,
    where ``approver_count`` comes from the linked ``ActorGroup.member_count``.
    """
    signals = list(signals)
    groups = _index_groups(signals)
    results: List[DetectorResult] = []

    for approval in signals:
        if not isinstance(approval, Approval):
            continue
        attrs = approval.attributes or {}
        b_score = float(attrs.get("bottleneck_score", 0.0))
        approver_count = _approver_count_for(approval, groups)
        pending = int(attrs.get("pending_count", 0))

        if approver_count == 0:
            continue
        if b_score <= PERMISSION_THRESHOLD:
            continue

        results.append(
            DetectorResult(
                detector_id=PERMISSION_BOTTLENECK_ID,
                signal_source=_SIGNAL_SOURCE,
                metric_value=round(b_score, 2),
                threshold=PERMISSION_THRESHOLD,
                raw_evidence={
                    "process_name": str(attrs.get("process_name", "")),
                    "pending_count": pending,
                    "approver_count": approver_count,
                    "bottleneck_score": b_score,
                },
            )
        )

    return results


#: The ports, keyed by the detector id they reproduce — so a test (or a future
#: concept-native runner) can pair each port with its original by id.
CONCEPT_NATIVE_DETECTORS = {
    APPROVAL_BOTTLENECK_ID: detect_approval_bottleneck,
    PERMISSION_BOTTLENECK_ID: detect_permission_bottleneck,
}

__all__ = [
    "detect_approval_bottleneck",
    "detect_permission_bottleneck",
    "CONCEPT_NATIVE_DETECTORS",
]
