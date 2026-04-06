from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Dict

@dataclass
class DetectorResult:
    detector_id: str
    signal_source: str
    metric_name: str
    metric_value: float
    threshold: float
    label: str
    raw_evidence: Dict[str, Any]

class IngestError(RuntimeError):
    pass
