"""
R16-C1 T4 — Provenance Guard: observed-beats-inferred ordering

Ensures weighting respects evidence provenance by enforcing that inferred
evidence cannot outrank directly-observed evidence through the weight and
priority mechanism (R16-C1 Section 2, AC5).

WHY THIS EXISTS
---------------
The Stack Builder lets customers assign a role and priority to each connected
system. These settings produce a source_weight (role × priority) applied to
that system's evidence contribution. The problem this module solves:

  A customer who gives an inferred-evidence source a high role/priority
  could inadvertently (or incorrectly) cause the system to treat inferred
  patterns as if they were directly measured facts — even ranking them above
  directly-observed evidence from a lower-priority source.

The spec (R16-C1 Section 2) is explicit:
  "The observed-beats-inferred ordering stands regardless of weighting."
  "Weighting tunes contribution within the rules; it never breaks them."

HOW IT WORKS
------------
Two complementary constraints enforce the provenance ordering:

1. Priority-nudge stripping for inferred evidence
   The source_weight is role × priority, clamped to [WEIGHT_MIN, WEIGHT_MAX].
   The priority nudge (+10%/−10%) rewards or penalises sources the customer
   marks as more or less important.  For INFERRED evidence this nudge must
   not apply — a customer's priority preference applies to direct observations,
   not to derived patterns.  The effective weight for inferred evidence is
   therefore capped at the base ROLE_WEIGHT (no priority boost beyond the
   role authority itself).

   Formally:
       effective_weight(observed) = source_weight      (unchanged)
       effective_weight(inferred) = min(source_weight, base_role_weight)

   This guarantees that at the same source, observed evidence always sees a
   weight ≥ the inferred evidence's weight.

2. Confidence ceiling for inferred evidence
   Inferred evidence cannot produce HIGH confidence from the scorer alone,
   regardless of how strong the computed proxy ratio is.  The ceiling is MEDIUM.
   This keeps inferred evidence "clearly secondary" as the spec requires.
   Cross-system corroboration (the corroboration engine) may still produce HIGH
   for an OVERALL finding — T4 only bounds what a single inferred detector
   result can assert on its own.

RELATIONSHIP TO T3
------------------
T3 (t3_ceiling_clamp.py) enforces ceilings based on the SOURCE ROLE (supporting
vs system_of_record) and source identity (Slack system_id).  T4 adds an
orthogonal axis: the EVIDENCE TYPE (observed vs inferred).  A finding can be:
  - Observed from a system_of_record → T3 does not clamp, T4 does not clamp
  - Inferred from a system_of_record → T3 does not clamp, T4 DOES clamp
  - Observed from a supporting role  → T3 clamps at MEDIUM, T4 does not add more
  - Inferred from a supporting role  → BOTH T3 and T4 apply (additive)

APPLICATION POINT
-----------------
Both functions are called from scorer.score() after _compute_confidence() and
before the T3 ceiling clamp, in the order:
    1. Apply T4 provenance weight cap   → effective_weight
    2. Re-compute confidence with effective_weight (or apply T4 ceiling)
    3. Apply T3 ceiling clamp

The score_debug dict exposes a ``t4_provenance`` key for full audit visibility.
"""
from __future__ import annotations

from typing import Optional

# ─────────────────────────────────────────────────────────────────────────────
# Provenance type constants
# ─────────────────────────────────────────────────────────────────────────────

#: Evidence produced by directly reading source-system records (counts,
#: delays, queue depths, etc.).  This is the default for all detectors.
PROVENANCE_OBSERVED: str = "observed"

#: Evidence derived from pattern inference, detector co-firing, or
#: relationship graph traversal rather than direct system-field reads.
PROVENANCE_INFERRED: str = "inferred"

#: All recognised provenance type values.
VALID_PROVENANCE_TYPES: frozenset = frozenset({PROVENANCE_OBSERVED, PROVENANCE_INFERRED})


# ─────────────────────────────────────────────────────────────────────────────
# Provenance ordering constants (mirror entity_relationships model values)
# ─────────────────────────────────────────────────────────────────────────────

#: Confidence ceiling for inferred evidence from the scorer alone.
#: Inferred evidence cannot assert HIGH confidence on its own.
INFERRED_CONFIDENCE_CEILING: str = "MEDIUM"

#: Confidence levels used in ordering checks.
_CONFIDENCE_ORDER: dict = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}


# ─────────────────────────────────────────────────────────────────────────────
# Core guard functions
# ─────────────────────────────────────────────────────────────────────────────

def apply_provenance_weight_cap(
    source_weight: float,
    base_role_weight: float,
    provenance_type: str,
) -> float:
    """Return the provenance-adjusted effective weight for this evidence.

    For **observed** evidence the weight is returned unchanged — full role ×
    priority contribution is allowed.

    For **inferred** evidence the priority nudge is stripped: the effective
    weight is capped at the base ROLE_WEIGHT (the authority the source role
    carries without the customer's priority preference on top).  This ensures
    that a customer marking an inferred-evidence source as "primary priority"
    cannot push its effective weight above what the role itself authorises for
    direct observations.

    Parameters
    ----------
    source_weight:
        The full computed weight (role × priority, clamped to [WEIGHT_MIN,
        WEIGHT_MAX]) from :func:`weighting_context.compute_source_weight`.
    base_role_weight:
        The authority of the source role alone, without the priority nudge.
        Equal to ``ROLE_WEIGHT.get(role, WEIGHT_NEUTRAL)``.
    provenance_type:
        ``"observed"`` or ``"inferred"``.  Any unknown value is treated as
        ``"observed"`` (fail-open so unknown provenance does not break runs).

    Returns
    -------
    float
        The effective weight to pass to scoring functions.  Always within
        ``[WEIGHT_MIN, WEIGHT_MAX]`` (the source_weight invariant is
        preserved for observed; for inferred it can only be ≤ source_weight).
    """
    if provenance_type != PROVENANCE_INFERRED:
        return source_weight  # observed or unknown: no cap

    # Inferred: cap at base role weight — priority nudge is stripped.
    return min(source_weight, base_role_weight)


def apply_provenance_confidence_ceiling(
    confidence: str,
    provenance_type: str,
) -> str:
    """Cap the confidence of inferred evidence at MEDIUM.

    Inferred evidence cannot assert HIGH confidence from the scorer alone.
    The corroboration engine may still reach HIGH for the overall finding if
    multiple systems corroborate — this ceiling applies only to the single
    inferred detector result's own scorer verdict.

    For observed evidence the confidence is returned unchanged.

    Parameters
    ----------
    confidence:
        The confidence string from the scorer (``"HIGH"``, ``"MEDIUM"``,
        ``"LOW"``).
    provenance_type:
        ``"observed"`` or ``"inferred"``.

    Returns
    -------
    str
        Clamped confidence.  Inferred evidence is at most ``"MEDIUM"``.
    """
    if provenance_type != PROVENANCE_INFERRED:
        return confidence

    if confidence == "HIGH":
        return INFERRED_CONFIDENCE_CEILING

    return confidence


def observed_beats_inferred(
    observed_effective_weight: float,
    inferred_effective_weight: float,
) -> bool:
    """Return True when the provenance ordering constraint is satisfied.

    The T4 invariant requires that at the same source, the effective weight
    applied to observed evidence is always greater than or equal to the
    effective weight applied to inferred evidence.

    This function is used in the audit trail and by tests to assert the
    constraint rather than silently depend on it.

    Parameters
    ----------
    observed_effective_weight:
        The effective weight that would be applied to observed evidence from
        a given source.
    inferred_effective_weight:
        The effective weight that would be applied to inferred evidence from
        the same source.

    Returns
    -------
    bool
        ``True`` when ``observed_effective_weight >= inferred_effective_weight``.
    """
    return observed_effective_weight >= inferred_effective_weight


def provenance_guard_debug(
    provenance_type: str,
    source_weight: float,
    base_role_weight: float,
    effective_weight: float,
    confidence_before_provenance: str,
    confidence_after_provenance: str,
) -> dict:
    """Build the ``t4_provenance`` score_debug sub-dict.

    Exposes the full T4 audit trail so any consumer (tests, auditors,
    UI debug panels) can verify the provenance ordering was honoured.

    Parameters
    ----------
    provenance_type:
        The evidence provenance type (``"observed"`` or ``"inferred"``).
    source_weight:
        The full source weight before the T4 cap.
    base_role_weight:
        The role-only weight (no priority nudge).
    effective_weight:
        The weight actually used in scoring (after T4 cap).
    confidence_before_provenance:
        Confidence computed from ``_compute_confidence()`` before T4 ceiling.
    confidence_after_provenance:
        Confidence after T4 provenance ceiling applied.

    Returns
    -------
    dict
        Structured audit dict included in ``score_debug["t4_provenance"]``.
    """
    weight_capped = effective_weight < source_weight
    confidence_clamped = confidence_after_provenance != confidence_before_provenance

    return {
        "provenance_type":              provenance_type,
        "source_weight":                round(source_weight, 4),
        "base_role_weight":             round(base_role_weight, 4),
        "effective_weight":             round(effective_weight, 4),
        "weight_capped":                weight_capped,
        "confidence_before_provenance": confidence_before_provenance,
        "confidence_after_provenance":  confidence_after_provenance,
        "confidence_clamped":           confidence_clamped,
        # Constraint assertion exposed for test transparency
        "observed_beats_inferred_holds": observed_beats_inferred(
            source_weight,          # observed from same source would use full weight
            effective_weight,       # inferred uses capped weight
        ),
    }
