"""
SF-2.1: Shared data models for the AgentIQ discovery algorithm.
All Sprint 2 modules import DetectorResult from here.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Literal


#: The provenance tiers the engine understands. The provenance guard
#: (provenance_guard.py) only distinguishes "inferred" from everything-else,
#: so any value outside this set would be silently treated as "observed" — a
#: future third tier (e.g. "corroborated") would be mis-ranked. We validate
#: against this set in DetectorResult.__post_init__ so an unrecognised tier
#: fails loudly at construction rather than being silently downgraded. Any new
#: tier must be added HERE and taught to provenance_guard.py at the same time.
VALID_PROVENANCE_TYPES: frozenset = frozenset({"observed", "inferred"})

try:
    from app.temporal import DetectorEvaluation
except ModuleNotFoundError:  # project-root execution uses backend as package
    from backend.app.temporal import DetectorEvaluation


TEMPORAL_EVALUATION_APPROACH = "Option A: detectors expose evaluate() separately from detect()."


@dataclass
class DetectorResult:
    """
    Output of a single detector firing.
    Matches the schema defined in SF-1.3.

    R16-C1 T4: provenance_type tracks whether the evidence is directly
    observed from source-system records ("observed") or derived from
    detector co-firing patterns or relationship inference ("inferred").
    Defaults to "observed" so all existing callers remain unaffected.
    The scorer uses this to enforce the observed-beats-inferred ordering
    per R16-C1 Section 2 — see provenance_guard.py.
    """
    detector_id: str        # e.g. "HANDOFF_FRICTION"
    signal_source: str      # e.g. "salesforce", "servicenow", "jira"
    metric_value: float     # the computed value that crossed the threshold
    threshold: float        # the threshold that was crossed
    raw_evidence: dict      # source data — must contain at least one number
    # R16-C1 T4: validated against VALID_PROVENANCE_TYPES in __post_init__.
    provenance_type: Literal["observed", "inferred"] = "observed"

    def __post_init__(self):
        if not isinstance(self.raw_evidence, dict):
            raise ValueError("raw_evidence must be a dict")
        if not any(isinstance(v, (int, float)) for v in self._all_values(self.raw_evidence)):
            raise ValueError("raw_evidence must contain at least one numeric value")
        # Fail loudly on an unrecognised provenance tier rather than letting the
        # provenance guard silently treat it as "observed" (see provenance_guard.py).
        if self.provenance_type not in VALID_PROVENANCE_TYPES:
            raise ValueError(
                f"provenance_type must be one of {sorted(VALID_PROVENANCE_TYPES)}, "
                f"got {self.provenance_type!r}"
            )

    def _all_values(self, d: dict):
        for v in d.values():
            if isinstance(v, dict):
                yield from self._all_values(v)
            else:
                yield v


def make_detector_evaluation(
    *,
    module_name: str,
    detector_id: str,
    signal_source: str,
    metric_value: float,
    threshold: float,
    fired: bool,
    raw_evidence: dict,
) -> DetectorEvaluation:
    """
    Build the temporal interface object for module-based detectors.

    T3-S10-A chose Option A: every detector exposes evaluate() for temporal
    capture, while detect() remains the thresholded opportunity API. Existing
    detectors are modules rather than classes, so this helper attaches a
    lightweight detector class to each module. snapshot_signals() can read
    SIGNAL_METRICS via getattr(evaluation.detector_cls, "SIGNAL_METRICS", []).
    """
    import sys

    detector_module = sys.modules[module_name]
    detector_cls = getattr(detector_module, "DETECTOR_CLS", None)
    if detector_cls is None:
        class_name = "".join(part.title() for part in detector_id.lower().split("_"))
        detector_cls = type(
            f"{class_name}Detector",
            (),
            {
                "DETECTOR_ID": detector_id,
                "SIGNAL_METRICS": getattr(detector_module, "SIGNAL_METRICS", []),
            },
        )
        setattr(detector_module, "DETECTOR_CLS", detector_cls)

    return DetectorEvaluation(
        detector_id=detector_id,
        detector_cls=detector_cls,
        signal_source=signal_source,
        metric_value=float(metric_value),
        threshold=float(threshold),
        fired=bool(fired),
        raw_evidence=dict(raw_evidence or {}),
    )


def detector_result_from_evaluation(evaluation: DetectorEvaluation) -> DetectorResult:
    """Convert a fired DetectorEvaluation back to the existing result shape."""
    return DetectorResult(
        detector_id=evaluation.detector_id,
        signal_source=evaluation.signal_source,
        metric_value=evaluation.metric_value,
        threshold=evaluation.threshold,
        raw_evidence=evaluation.raw_evidence,
    )
