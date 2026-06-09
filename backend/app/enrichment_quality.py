"""ENT-3 / T3-S15-A — Preliminary quality gate (Section 4).

``evaluate_preliminary_status`` decides whether an enriched finding still needs
analyst review before it can be presented as confirmed on the executive
dashboard. It returns ``(preliminary, reason)`` where ``preliminary=True`` means
"analyst review required" and ``reason`` is the human-readable explanation the
frontend evidence trace renders (T6).

Three gates are checked IN ORDER — the order is significant so the surfaced
reason reflects the most fundamental unmet condition:

  Gate 1 — baseline maturity:  run_count < 10                      (AC6)
  Gate 2 — entity resolution:  any entity not 'resolved'           (AC7)
  Gate 3 — confidence:         average resolution_confidence < 0.8

Only when all three pass does it return ``(False, None)`` — confirmed (AC8).

The function is pure and deterministic given the same enrichment object + run
count, and tolerates entities supplied as either dicts or objects. An empty
entity list is treated as confidence 0 (preliminary) rather than raising.
The gate is advisory: it annotates the finding, it never alters scoring.
"""
from __future__ import annotations

from typing import Any, List, Optional, Tuple

# Number of completed runs required before baseline context is considered mature.
MIN_BASELINE_RUNS = 10
# Minimum average entity-resolution confidence for a confirmed finding.
MIN_AVG_CONFIDENCE = 0.8


def _entities_of(opp_enrichment: Any) -> List[Any]:
    """Return the entity list from an OppEnrichment-like object or dict."""
    if opp_enrichment is None:
        return []
    if isinstance(opp_enrichment, dict):
        entities = opp_enrichment.get("entities")
    else:
        entities = getattr(opp_enrichment, "entities", None)
    return list(entities) if entities else []


def _field(entity: Any, key: str, default: Any = None) -> Any:
    if isinstance(entity, dict):
        return entity.get(key, default)
    return getattr(entity, key, default)


def evaluate_preliminary_status(
    opp_enrichment: Any,
    run_count: Optional[int],
    org_id: Optional[str] = None,
) -> Tuple[bool, Optional[str]]:
    """Evaluate the three preliminary quality gates in order.

    Returns ``(preliminary, reason)``. ``preliminary=True`` means analyst review
    is required; ``reason`` is the explanation shown in the evidence trace.
    Returns ``(False, None)`` only when all three gates pass.
    """
    # Gate 1 — baseline maturity. A missing run_count is treated as immature.
    count = run_count if isinstance(run_count, int) and not isinstance(run_count, bool) else 0
    if count < MIN_BASELINE_RUNS:
        return True, (
            f"Baseline context is still accumulating "
            f"({count} of {MIN_BASELINE_RUNS} runs completed)"
        )

    entities = _entities_of(opp_enrichment)

    # Gate 2 — entity resolution.
    unresolved = [e for e in entities if _field(e, "resolution_status") != "resolved"]
    if unresolved:
        return True, (
            f"{len(unresolved)} entities require resolution before findings are confirmed"
        )

    # Gate 3 — average resolution confidence. Empty list => 0 => preliminary.
    if entities:
        total = 0.0
        for e in entities:
            try:
                total += float(_field(e, "resolution_confidence", 0) or 0)
            except (TypeError, ValueError):
                total += 0.0
        avg_confidence = total / len(entities)
    else:
        avg_confidence = 0.0

    if avg_confidence < MIN_AVG_CONFIDENCE:
        return True, (
            f"Entity confidence is {avg_confidence:.2f} — "
            f"configure entity overlay for higher precision"
        )

    return False, None  # all gates passed — finding is confirmed
