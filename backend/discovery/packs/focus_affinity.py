"""
R16-C2 — T1: FOCUS_AFFINITY — Discovery Focus → finding affinities
Track C — Discovery Product & Packs | Release 1.6

The Discovery Focus lens (``focus_id``) is captured in Stack Builder and
persisted on the run, but historically it did *not* shape detector
execution, scoring, context assembly, or ranking — choosing one focus over
another produced materially the same discovery output.

This module is the canonical, backend-owned mapping that connects each
Discovery Focus tile to the detector / finding types it should *emphasise*
during discovery. It is the single source of truth consumed by the scoring,
ranking, and (future) context-assembly paths (R16-C2 T2/T3/T4). The frontend
does not decide discovery behaviour — it only captures the focus selection.

Design rules (per Section 2 of the story):
  * Emphasis, NOT exclusion. An affinity biases ranking toward the findings
    most relevant to the chosen lens; it never hides findings outside the
    lens. A strongly corroborated HIGH finding outside the focus is still
    surfaced (enforced downstream in T3). This module only declares *which*
    detector ids each focus emphasises.
  * ``enterprise_wide`` carries no affinity bias — it is the full, unweighted
    view (``None``).
  * Affinities reference stable canonical ``DETECTOR_ID`` constants (the
    uppercase ids each detector module exposes, e.g. ``APPROVAL_BOTTLENECK``),
    NOT UI tile titles, so future copy changes cannot break discovery
    behaviour.
  * Deterministic. Same focus + same data => same emphasis ordering. The
    affinity sets are static module-level data; lookups are pure functions.
  * Unknown detector ids degrade safely — they simply match no focus and are
    therefore never emphasised (and never crash a run).

Product boundaries encoded here (Section 2 / Section 3):
  * Approval gates, compliance deadlines, covenant tracking, permission
    bottlenecks, SLA breaches, and regulatory control points belong under
    ``approvals_compliance`` — *the gate is the bottleneck*.
  * Ownership friction, lost handoffs, cross-system echo, incident/change
    correlation, and sync issues belong under ``cross_system_handoffs`` —
    *the handoff between teams/systems is the bottleneck*.

Detector ids are drawn from every registered pack (see
``discovery/packs/pack_config.py``): Service Cloud, nCino, STRS Benefits,
SQL Server operational signals, GitHub engineering, Enterprise operations,
and Cloud operations (MSP-B6). Every non-enterprise detector appears in at
least one focus.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Module-level import with fallback so the run-KV loader works from both the
# in-package path (app.db) and the root path, mirroring weighting_context.py.
try:
    from app.db import run_kv_get
except ModuleNotFoundError:
    try:
        from backend.app.db import run_kv_get
    except ModuleNotFoundError:
        run_kv_get = None  # type: ignore[assignment]

# ── Focus ids ─────────────────────────────────────────────────────────────────
# Mirrors the frontend ``FocusId`` union (frontend/src/types/stack_builder.ts).
# These are the only seven valid Discovery Focus values.

FOCUS_MEMBER_CUSTOMER_SERVICE = "member_customer_service"
FOCUS_CORE_OPERATIONS = "core_operations"
FOCUS_APPROVALS_COMPLIANCE = "approvals_compliance"
FOCUS_CROSS_SYSTEM_HANDOFFS = "cross_system_handoffs"
FOCUS_BACK_OFFICE_PRODUCTIVITY = "back_office_productivity"
FOCUS_ENGINEERING_CHANGE = "engineering_change"
FOCUS_ENTERPRISE_WIDE = "enterprise_wide"

# Explicit no-op affinity used by enterprise_wide. Keeping this named constant
# makes the product contract clear: enterprise_wide intentionally applies no
# focus modifier and is the full, unbiased baseline view.
FOCUS_NO_AFFINITY_BIAS: Optional[Tuple[str, ...]] = None


# ── Affinity mapping ───────────────────────────────────────────────────────────
# Each focus maps to a deterministic, ordered tuple of canonical DETECTOR_ID
# strings it emphasises. ``enterprise_wide`` maps to None (no bias).
#
# A detector may appear under more than one focus when it genuinely serves two
# lenses — affinity is about emphasis, not partitioning. Overlaps are
# deliberate and documented inline.

FOCUS_AFFINITY: Dict[str, Optional[Tuple[str, ...]]] = {
    # Front-line, customer/member-facing service quality.
    FOCUS_MEMBER_CUSTOMER_SERVICE: (
        "REPETITIVE_AUTOMATION",   # repetitive front-line case handling
        "KNOWLEDGE_GAP",           # agents lack knowledge to serve members
        "APPLICATION_STALL",       # member application processing stalls
        "FSC_SERVICING_REQUEST_RECURRENCE",  # FSC: repeat servicing requests per household
    ),
    # Operational throughput — queues, volume, resolution speed.
    FOCUS_CORE_OPERATIONS: (
        "DB_QUEUE_DEPTH_ELEVATED",      # queue backlog
        "DB_TICKET_VOLUME_SURGE",       # throughput / volume surge
        "ENT_INCIDENT_RESOLUTION_LAG",  # incident resolution throughput
        "SPREADING_BOTTLENECK",         # core processing throughput
        "RECURRING_RESOLUTION_LOOP",    # NOC: repeated same-resolution toil
        "ALERT_TRIAGE_TOIL",            # NOC: high-volume trivially-resolved alerts
        "QUEUE_AGEING",                 # NOC: operational queue ageing vs baseline
        "FSC_SERVICE_QUEUE_AGEING",     # FSC: service-process queue ageing vs own baseline
    ),
    # The gate is the bottleneck: approval gates, compliance deadlines,
    # covenant tracking, permissions, SLA breaches, regulatory control points.
    FOCUS_APPROVALS_COMPLIANCE: (
        "APPROVAL_BOTTLENECK",          # approval gate
        "PERMISSION_BOTTLENECK",        # permission bottleneck
        "COVENANT_TRACKING_GAP",        # covenant tracking
        "DB_SLA_BREACH_RATE",           # SLA breach
        "ENT_SLA_BREACH_BY_TEAM",       # SLA breach
        "BENEFIT_ELECTION_DEADLINE",    # compliance deadline
        "DISBURSEMENT_OVERDUE",         # deadline / overdue obligation
        "DISABILITY_REVIEW_BOTTLENECK", # regulatory review control point
        "CHECKLIST_BOTTLENECK",         # compliance document control point
        "FSC_APPROVAL_REVIEW_CYCLE",    # FSC: approval + compliance/suitability review dwell
    ),
    # The handoff between teams/systems is the bottleneck.
    FOCUS_CROSS_SYSTEM_HANDOFFS: (
        "HANDOFF_FRICTION",                  # ownership friction / lost handoffs
        "CROSS_SYSTEM_ECHO",                 # cross-system echo / sync issues
        "INTEGRATION_CONCENTRATION",         # integration / sync concentration
        "ENT_CHANGE_INCIDENT_CORRELATION",   # incident/change correlation
        "LOAN_ORIGINATION_ROUTING_FRICTION", # cross-stage routing/handoff friction
        "REASSIGNMENT_PING_PONG",            # NOC: incident bounced between groups/queues
        "SHARED_CI_HOTSPOT",                 # NOC: multi-service incidents on one shared CI
        "FSC_REFERRAL_HANDOFF_FRICTION",     # FSC: referrals bouncing between teams
    ),
    # Internal staff productivity — repetitive manual work, document & data prep.
    FOCUS_BACK_OFFICE_PRODUCTIVITY: (
        "REPETITIVE_AUTOMATION",   # repetitive manual back-office work
        "CHECKLIST_BOTTLENECK",    # back-office document checklist
        "SPREADING_BOTTLENECK",    # back-office financial spreading
        "KNOWLEDGE_GAP",           # internal knowledge / process gaps
        "FSC_CROSS_OBJECT_REWORK",  # FSC: same fact maintained on several objects
    ),
    # Engineering change & release health.
    FOCUS_ENGINEERING_CHANGE: (
        "GITHUB_PR_REVIEW_BOTTLENECK",     # PR review bottleneck
        "GITHUB_COMMIT_CONCENTRATION",     # commit concentration risk
        "GITHUB_STALE_BRANCHES",           # stale branch accumulation
        "ENT_CHANGE_INCIDENT_CORRELATION", # change-driven incidents
    ),
    # The unbiased, full view — no affinity bias.
    FOCUS_ENTERPRISE_WIDE: FOCUS_NO_AFFINITY_BIAS,
}

# Frozen set of the seven valid focus ids, derived from the mapping so the two
# can never drift apart.
VALID_FOCUS_IDS = frozenset(FOCUS_AFFINITY.keys())


# ── Public API ──────────────────────────────────────────────────────────────

def _normalize(focus_id: Optional[str]) -> Optional[str]:
    """Lower/strip a focus id for tolerant matching. None passes through."""
    if focus_id is None:
        return None
    return focus_id.strip().lower()


def is_valid_focus(focus_id: Optional[str]) -> bool:
    """Return True iff ``focus_id`` is one of the seven canonical focus ids."""
    return _normalize(focus_id) in VALID_FOCUS_IDS


def is_enterprise_wide_focus(focus_id: Optional[str]) -> bool:
    """Return True when ``focus_id`` is the explicit unbiased full-view mode."""
    return _normalize(focus_id) == FOCUS_ENTERPRISE_WIDE


def get_focus_affinity(focus_id: Optional[str]) -> Optional[Tuple[str, ...]]:
    """
    Return the ordered tuple of emphasised DETECTOR_IDs for ``focus_id``.

    Returns ``None`` for ``enterprise_wide`` (no affinity bias — the full,
    unweighted view).

    Degrades safely: an unknown / None focus id is treated as *no bias*
    (returns ``None``) and logs a WARNING so misconfiguration is visible in
    logs rather than silently biasing or crashing a run — the same fail-soft
    convention as ``pack_config.get_pack()``.
    """
    norm = _normalize(focus_id)
    if norm == FOCUS_ENTERPRISE_WIDE:
        return FOCUS_NO_AFFINITY_BIAS
    if norm in FOCUS_AFFINITY:
        return FOCUS_AFFINITY[norm]
    logger.warning(
        "get_focus_affinity: unrecognized focus_id %r — treating as no bias "
        "(enterprise_wide). Valid focus ids: %s",
        focus_id, sorted(VALID_FOCUS_IDS),
    )
    return None


def has_affinity_bias(focus_id: Optional[str]) -> bool:
    """
    Return True when ``focus_id`` carries an affinity bias.

    ``enterprise_wide`` (and any unknown/None focus, which degrades to the
    unbiased view) returns False.
    """
    if is_enterprise_wide_focus(focus_id):
        return False
    return get_focus_affinity(focus_id) is not None


def detector_matches_focus(focus_id: Optional[str], detector_id: Optional[str]) -> bool:
    """
    Return True when ``detector_id`` is emphasised by ``focus_id``.

    This is the membership check the scoring/ranking path uses to decide
    whether a finding should be emphasised under the chosen lens.

    Degrades safely:
      * ``enterprise_wide`` / unknown / None focus => no bias => always False
        (every finding is treated equally — the unbiased view).
      * Unknown / None ``detector_id`` => False (matches no focus, so it is
        never emphasised, and never errors).
    """
    affinity = get_focus_affinity(focus_id)
    if affinity is None or detector_id is None:
        return False
    return detector_id in affinity


def list_focus_ids() -> List[str]:
    """Return all seven valid focus ids (deterministic order)."""
    return list(FOCUS_AFFINITY.keys())


def all_affinity_detector_ids() -> Tuple[str, ...]:
    """
    Return the de-duplicated set of every DETECTOR_ID referenced by any focus,
    in deterministic (sorted) order. Useful for validation and tests.
    """
    seen: set = set()
    for detectors in FOCUS_AFFINITY.values():
        if detectors:
            seen.update(detectors)
    return tuple(sorted(seen))


# ── R16-C2 T2: emphasis at ranking time ─────────────────────────────────────

# Sort rank for a finding that the focus emphasises. Lower sorts earlier, so
# emphasised findings (0) rank ahead of non-emphasised findings (1). These are
# the only two values the ranking primitive yields — focus orders, it does not
# score, and it never censors (emphasis, not exclusion).
FOCUS_EMPHASIS_RANK = 0
FOCUS_NEUTRAL_RANK = 1


def focus_emphasis_rank(focus_id: Optional[str], detector_id: Optional[str]) -> int:
    """
    Deterministic ranking primitive: return ``FOCUS_EMPHASIS_RANK`` (0) when
    ``detector_id`` is emphasised by ``focus_id``, else ``FOCUS_NEUTRAL_RANK`` (1).

    For ``enterprise_wide`` (and any unknown/None focus, which degrades to the
    unbiased view) every detector returns the neutral rank, so ordering is
    identical to the unbiased baseline — no bias is applied.
    """
    return FOCUS_EMPHASIS_RANK if detector_matches_focus(focus_id, detector_id) else FOCUS_NEUTRAL_RANK


def build_focus_emphasis(focus_id: Optional[str], detector_id: Optional[str]) -> Dict[str, Any]:
    """
    Build the additive, contract-safe ``focus_emphasis`` annotation attached to
    each opportunity by the discovery runner.

    The annotation is purely descriptive emphasis metadata — it never replaces
    or mutates impact, effort, confidence, tier, roadmap stage, evidence ids,
    or corroboration fields. It records:

      * ``focus_id``       — the focus the run was launched with (or None),
      * ``matched``        — whether this detector is emphasised by that focus,
      * ``rank``           — the deterministic ranking primitive (0 emphasised,
                             1 neutral) consumed by the shared ranking utility,
      * ``affinity``       — the focus's affinity detector ids (() for no bias),
      * ``rationale``      — a short, human-readable explanation for the UI/debug.

    Always returns a fully-populated dict (every key present) so the field is
    contract-stable regardless of focus.
    """
    norm = _normalize(focus_id)
    affinity = get_focus_affinity(focus_id)
    matched = detector_matches_focus(focus_id, detector_id)

    if affinity is None:
        # enterprise_wide or unknown/None focus -> the full, unbiased view.
        if is_enterprise_wide_focus(focus_id):
            rationale = (
                "Enterprise-wide focus: no affinity bias; full unweighted view "
                "across selected systems and active pack."
            )
        elif norm is None:
            rationale = "No focus selected: no affinity bias — full unweighted view."
        else:
            rationale = "Unrecognised focus: no affinity bias — full unweighted view."
    elif matched:
        rationale = f"Emphasised by '{norm}' focus affinity."
    else:
        rationale = f"Outside '{norm}' focus affinity — surfaced but not emphasised."

    return {
        "focus_id": focus_id,
        "matched": matched,
        "rank": focus_emphasis_rank(focus_id, detector_id),
        "affinity": list(affinity) if affinity else [],
        "rationale": rationale,
    }


def load_focus_for_run(run_id: Optional[str]) -> Optional[str]:
    """
    Load the persisted ``focus_id`` for a run from the run-KV store.

    Reads the dedicated ``focus_id`` KV entry written by the Stack Builder
    launch endpoint, falling back to ``setup_context["focus_id"]``. Returns
    ``None`` when no run id, no KV accessor, or no stored focus is available.

    Never raises — all failures return ``None`` (the unbiased view) so the
    discovery pipeline always has a usable value. Mirrors
    ``weighting_context.load_for_run`` for the focus dimension.
    """
    if not run_id or run_kv_get is None:
        return None
    try:
        focus = run_kv_get("focus_id", run_id)
        if isinstance(focus, str) and focus.strip():
            return focus
        ctx = run_kv_get("setup_context", run_id)
        if isinstance(ctx, dict):
            ctx_focus = ctx.get("focus_id")
            if isinstance(ctx_focus, str) and ctx_focus.strip():
                return ctx_focus
        return None
    except Exception as exc:  # noqa: BLE001 — never break the pipeline
        logger.warning(
            "R16-C2: failed to load focus_id for run_id=%s (non-blocking): %s",
            run_id, exc,
        )
        return None
