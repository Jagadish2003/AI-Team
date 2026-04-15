"""
SF-2.1: Shared data models for the AgentIQ discovery algorithm.
All Sprint 2 modules import DetectorResult from here.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any


@dataclass
class DetectorResult:
    """
    Output of a single detector firing.
    Matches the schema defined in SF-1.3.
    """
    detector_id: str        # e.g. "HANDOFF_FRICTION"
    signal_source: str      # e.g. "salesforce", "servicenow", "jira"
    metric_value: float     # the computed value that crossed the threshold
    threshold: float        # the threshold that was crossed
    raw_evidence: dict      # source data — must contain at least one number

    def __post_init__(self):
        if not isinstance(self.raw_evidence, dict):
            raise ValueError("raw_evidence must be a dict")
        if not any(isinstance(v, (int, float)) for v in self._all_values(self.raw_evidence)):
            raise ValueError("raw_evidence must contain at least one numeric value")

    def _all_values(self, d: dict):
        for v in d.values():
            if isinstance(v, dict):
                yield from self._all_values(v)
            else:
                yield v
