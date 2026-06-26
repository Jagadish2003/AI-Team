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

R16-C2 T3 -- Emphasis, not exclusion guardrail:
  Focus is a lens, not a blindfold. A HIGH finding with corroboration support
  remains surfaced even when it is outside the chosen focus. The full input list
  is still returned; the guardrail only prevents focus affinity from dominating
  serious confidence/corroboration signals.

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

try:
    from ..packs.corroboration_rules import is_elevating_rule
except ImportError:  # pragma: no cover - defensive, keep ranking usable standalone
    _ELEVATING_CORROBORATION_RULE_IDS = {
        "COR-01",
        "COR-02",
        "COR-03",
        "COR-04",
        "COR-06",
        "COR-07",
    }

    def is_elevating_rule(rule_id):  # type: ignore[misc]
        return str(rule_id).upper() in _ELEVATING_CORROBORATION_RULE_IDS

TIER_ORDER: Dict[str, int] = {
    "Quick Win": 1,
    "Strategic": 2,
    "Complex":   3,
}

SURFACE_GUARDRAIL_RANK = 0
STANDARD_SURFACE_RANK = 1

_NON_ELEVATING_SOURCE_MARKERS = (
    "supporting only",
    "single source",
)


def _string_values(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if item is not None]
    return []


def _has_elevating_corroboration_rule(opp: Dict[str, Any]) -> bool:
    rule_ids = _string_values(opp.get("corroboration_rule_ids"))
    # Some callers work with the raw CorroborationResult shape before adapter
    # materialisation, where the field is named rule_ids.
    rule_ids.extend(_string_values(opp.get("rule_ids")))
    return any(is_elevating_rule(rule_id) for rule_id in rule_ids)


def _has_real_corroboration_source(opp: Dict[str, Any]) -> bool:
    sources = _string_values(opp.get("corroboration_sources"))
    for source in sources:
        normalised = source.strip().lower()
        if not normalised:
            continue
        if any(marker in normalised for marker in _NON_ELEVATING_SOURCE_MARKERS):
            continue
        return True
    return False


def is_high_well_corroborated(opp: Dict[str, Any]) -> bool:
    """
    Return True for the R16-C2 T3 protected case: a HIGH-confidence finding with
    explicit corroboration support.

    The check uses fields already carried through discovery: rule ids, triple
    corroboration, source chips, and older pack-specific corroborated markers.
    Non-elevating rule/source labels such as Slack-only or single-source do not
    qualify by themselves.
    """
    if str(opp.get("confidence", "")).upper() != "HIGH":
        return False
    if bool(opp.get("triple_corroboration", False)):
        return True
    if _has_elevating_corroboration_rule(opp):
        return True
    if bool(opp.get("corroborated", False)) and _has_real_corroboration_source(opp):
        return True
    return _has_real_corroboration_source(opp)


def _surface_guardrail_rank(opp: Dict[str, Any]) -> int:
    return SURFACE_GUARDRAIL_RANK if is_high_well_corroborated(opp) else STANDARD_SURFACE_RANK


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
    annotation, the leading focus term is constant across all opportunities.
    The R16-C2 T3 surface term only differs for HIGH well-corroborated findings,
    keeping legacy order unchanged for ordinary opportunities.
    """
    surface   = _surface_guardrail_rank(opp)
    emphasis  = _emphasis_rank(opp, focus_id)
    tier_rank = TIER_ORDER.get(opp.get("tier", "Complex"), 3)
    net_value = opp.get("impact", 0) - opp.get("effort", 10)
    effort    = opp.get("effort", 10)
    return (surface, emphasis, tier_rank, -net_value, effort)


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
