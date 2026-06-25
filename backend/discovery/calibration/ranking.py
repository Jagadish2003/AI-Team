"""
SF-3.3 — Shared Ranking Utility

The production ranking function used by:
  - calibrator.py  (algo_top5 selection)
  - track_a_adapter.py  (opportunity ordering in seed output)
  - runner.py  (opportunity ordering in runner payload)

Ranking logic (from Sprint 3 review — single definition, reused everywhere):
  Primary:   focus emphasis — findings matching the selected Discovery Focus
             affinity rank ahead of findings outside it (R16-C2 T2)
  Secondary: tier order — Quick Win (1) > Strategic (2) > Complex (3)
  Tertiary:  (impact - effort) desc within tier — higher net value ranks first
  Quaternary:effort asc — prefer lower delivery effort on ties

R16-C2 T2 — Discovery Focus emphasis:
  Focus is applied as *emphasis, not exclusion*. A focus reorders findings so
  the ones matching its affinity surface higher than they would in the
  unbiased enterprise-wide view; nothing is filtered out. When no focus is
  given (or ``enterprise_wide`` / an unknown focus), every finding receives the
  same neutral emphasis rank, so the ordering is byte-for-byte identical to the
  historical tier/net-value/effort ranking — fully backward compatible.

  Determinism: emphasis is a pure function of (focus_id, detector_id). Same
  data + same focus => identical ordering, every run. No LLM, no randomness.

This module is the single source of truth. Any change to ranking logic
must be made here and will propagate to all three consumers.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

try:
    from ..packs.focus_affinity import focus_emphasis_rank, FOCUS_NEUTRAL_RANK
except ImportError:  # pragma: no cover - defensive, keep ranking usable standalone
    FOCUS_NEUTRAL_RANK = 1

    def focus_emphasis_rank(focus_id, detector_id):  # type: ignore[misc]
        return FOCUS_NEUTRAL_RANK

TIER_ORDER: Dict[str, int] = {
    "Quick Win": 1,
    "Strategic": 2,
    "Complex":   3,
}


def _emphasis_rank(opp: Dict[str, Any], focus_id: Optional[str]) -> int:
    """
    Resolve the focus-emphasis rank for an opportunity (0 = emphasised, sorts
    first; 1 = neutral).

    Two sources, in precedence order:
      1. An explicit ``focus_id`` passed to ranking — recomputed live against
         the opportunity's top-level ``detector_id`` when present.
      2. The additive ``focus_emphasis`` annotation the runner already attached
         to the opportunity. This is the fallback used when no focus_id is
         threaded through, AND when a focus_id is given but ``detector_id`` is
         not at the top level (e.g. the Track A seed opp, where it is nested
         under ``_debug``).

    Falls back to the neutral rank when neither is available, preserving the
    historical (focus-unaware) ordering exactly.
    """
    if focus_id is not None:
        detector_id = opp.get("detector_id")
        if detector_id is not None:
            return focus_emphasis_rank(focus_id, detector_id)
        # focus_id given but detector_id is nested (Track A seed) — fall through
        # to the annotation the runner already computed for this run's focus.
    fe = opp.get("focus_emphasis")
    if isinstance(fe, dict) and "rank" in fe:
        try:
            return int(fe["rank"])
        except (TypeError, ValueError):
            return FOCUS_NEUTRAL_RANK
    return FOCUS_NEUTRAL_RANK


def rank_key(opp: Dict[str, Any], focus_id: Optional[str] = None):
    """
    Sort key for a single opportunity dict.

    Usage:
        sorted(opportunities, key=rank_key)                      # focus-unaware
        sorted(opportunities, key=lambda o: rank_key(o, focus))  # focus-aware

    When ``focus_id`` is None and the opportunity carries no ``focus_emphasis``
    annotation, the leading emphasis term is constant across all opportunities,
    so the result orders identically to the original (tier, -net_value, effort)
    key.
    """
    emphasis  = _emphasis_rank(opp, focus_id)
    tier_rank = TIER_ORDER.get(opp.get("tier", "Complex"), 3)
    net_value = opp.get("impact", 0) - opp.get("effort", 10)
    effort    = opp.get("effort", 10)
    return (emphasis, tier_rank, -net_value, effort)


def rank_opportunities(
    opportunities: List[Dict[str, Any]],
    focus_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Return a new list sorted by the production ranking function.
    Does not modify the input list.

    ``focus_id`` is optional and additive: pass it to apply Discovery Focus
    emphasis (R16-C2 T2). Omitting it preserves the original ranking behavior.
    Python's sort is stable, so ties beyond the key keep input order
    deterministically.
    """
    return sorted(opportunities, key=lambda o: rank_key(o, focus_id))
