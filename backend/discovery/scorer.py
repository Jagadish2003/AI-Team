"""
SF-2.6 stub — Scorer.
Implement score() using SF-1.4 factor tables and tier rules.
"""
from __future__ import annotations
from typing import Dict, Any
from .models import DetectorResult


def score(detector_result: DetectorResult) -> Dict[str, Any]:
    """
    Convert a DetectorResult into Impact, Effort, Confidence, Tier.
    Returns dict with keys: impact, effort, confidence, tier.
    SF-2.6 implements this function using SF-1.4 rubric.
    """
    # stub
    return {
        "impact": 5,
        "effort": 3,
        "confidence": "MEDIUM",
        "tier": "Quick Win",
    }
