"""
SF-3.3 — Shared Ranking Utility

The production ranking function used by:
  - calibrator.py  (algo_top5 selection)
  - track_a_adapter.py  (opportunity ordering in seed output)
  - runner.py  (opportunity ordering in runner payload)

Ranking logic (from Sprint 3 review — single definition, reused everywhere):
  Primary:   tier order — Quick Win (1) > Strategic (2) > Complex (3)
  Secondary: (impact - effort) desc within tier — higher net value ranks first
  Tertiary:  effort asc — prefer lower delivery effort on ties

This module is the single source of truth. Any change to ranking logic
must be made here and will propagate to all three consumers.
"""
from __future__ import annotations

from typing import Any, Dict, List

TIER_ORDER: Dict[str, int] = {
    "Quick Win": 1,
    "Strategic": 2,
    "Complex":   3,
}


def rank_key(opp: Dict[str, Any]):
    """
    Sort key for a single opportunity dict.

    Usage:
        sorted(opportunities, key=rank_key)
    """
    tier_rank = TIER_ORDER.get(opp.get("tier", "Complex"), 3)
    net_value = opp.get("impact", 0) - opp.get("effort", 10)
    effort    = opp.get("effort", 10)
    return (tier_rank, -net_value, effort)


def rank_opportunities(opportunities: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Return a new list sorted by the production ranking function.
    Does not modify the input list.
    """
    return sorted(opportunities, key=rank_key)
