"""
SF-2.7 stub — Evidence Builder.
Implement build_evidence() using SF-1.5 schema.
"""
from __future__ import annotations
from typing import List, Dict, Any, Callable, Optional
from .models import DetectorResult


def build_evidence(
    detector_result: DetectorResult,
    opportunity: Dict[str, Any],
    id_factory: Optional[Callable[[], str]] = None,
) -> List[Dict[str, Any]]:
    """
    Convert raw_evidence from DetectorResult into structured evidence objects.
    id_factory: optional callable for deterministic IDs in tests.
    SF-2.7 implements this function using SF-1.5 schema.
    Returns [] if evidence cannot be constructed.
    """
    return []  # stub
