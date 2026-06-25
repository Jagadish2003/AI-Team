"""
R16-C1 T1/T2 — Stack Builder Weighting Context

Loads per-system role and priority from a run's persisted Stack Builder
setup context and makes those values available to the scorer and
corroboration engine.

This module is the single entry point for all weighting data. Callers
never read run_kv_get("setup_context", run_id) directly — they go through
load_for_run(), which handles the fallback to neutral behavior when no
weighting context exists (backward compatibility for older runs that pre-
date Stack Builder weighting).

──────────────────────────────────────────────────────────────────────────
R16-C1 T2: Deterministic ROLE_WEIGHT + bounded priority modulation
──────────────────────────────────────────────────────────────────────────
The source weight for a system is a single float applied to that system's
evidence contribution in the scorer.  It is the product of two terms:

    source_weight = ROLE_WEIGHT[role] × PRIORITY_NUDGE[priority]
                    clamped to [WEIGHT_MIN, WEIGHT_MAX]

ROLE_WEIGHT (from R16-C1 Section 2 — authoritative, testable):
    system_of_record        → 1.0   (full authority)
    workflow_system         → 0.8   (process authority)
    operational_signal_source → 0.6 (current-app alias for supporting)
    documentation_system    → 0.6   (supporting interpretation source)
    engineering_change_system → 0.8 (change/process authority)
    supplementary           → 0.6   (contributes, does not lead)

PRIORITY_NUDGE (bounded — cannot override role authority):
    primary   → ×1.10
    secondary → ×1.00  (no change)
    tertiary  → ×0.90

Combined weight is clamped to [0.50, 1.10] so:
  • No source can be discounted below 50 % of neutral.
  • No source can exceed 110 % of neutral (prevents a high-priority
    supporting system from equalling a system-of-record).

CRITICAL: weighting modulates WITHIN the existing rules.  It changes how
much an evidence source contributes; it does NOT let a Supporting system
reach HIGH alone, and does NOT lift the Slack MEDIUM ceiling.  Those
hard-rule clamps are enforced in T3.

Role / priority constants mirror the values stored by the Stack Builder
frontend (routes_stack_builder_launch.py → LaunchRequest.weightings).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# R16-C1 T2 — Deterministic weight tables  (Section 2 of the spec)
# ─────────────────────────────────────────────────────────────────────────────

#: Authority multiplier per role (R16-C1 Section 2 — named, testable constants).
#: The current app uses more granular role strings than the three-term doc model;
#: they are mapped explicitly here so a reviewer can audit every mapping.
ROLE_WEIGHT: Dict[str, float] = {
    "system_of_record":          1.0,   # full authority — doc canonical
    "workflow_system":           0.8,   # process authority — doc canonical
    "operational_signal_source": 0.6,   # current-app alias for supporting
    "documentation_system":      0.6,   # current-app role: supports interpretation, does not lead
    "engineering_change_system": 0.8,   # current-app role: change/process authority
    "supporting":                0.6,   # doc canonical supporting
    "supplementary":             0.6,   # current-app alias; same as supporting
}

#: The neutral/fallback weight used when a system has no configured role.
WEIGHT_NEUTRAL: float = 1.0

#: Hard floor — no source can ever be discounted below this fraction.
WEIGHT_MIN: float = 0.50

#: Hard ceiling — no source can exceed this fraction of neutral evidence weight.
#: Keeps a high-priority supporting system below a system-of-record at 1.0.
WEIGHT_MAX: float = 1.10

#: Priority applies a bounded nudge ON TOP OF the role weight.
#: Range is ±10 % so it shifts the result without overturning the role authority.
PRIORITY_NUDGE: Dict[str, float] = {
    "primary":   1.10,   # additional emphasis
    "secondary": 1.00,   # no change
    "tertiary":  0.90,   # de-emphasis
}

#: Default nudge when priority is not set.
PRIORITY_NUDGE_DEFAULT: float = 1.00


def compute_source_weight(role: str, priority: str) -> float:
    """Return the bounded source weight for a given *role* / *priority* pair.

    This is the single, authoritative calculation used by the scorer and
    corroboration engine.  The result is deterministic: the same role and
    priority always produce the same weight.

    Calculation
    -----------
    ::

        raw = ROLE_WEIGHT.get(role, WEIGHT_NEUTRAL)
              × PRIORITY_NUDGE.get(priority, PRIORITY_NUDGE_DEFAULT)
        source_weight = clamp(raw, WEIGHT_MIN, WEIGHT_MAX)

    Parameters
    ----------
    role:
        Role string as stored by the Stack Builder, e.g.
        ``'system_of_record'``, ``'operational_signal_source'``.
    priority:
        Priority string as stored by the Stack Builder, e.g.
        ``'primary'``, ``'secondary'``, ``'tertiary'``.

    Returns
    -------
    float
        A weight in ``[WEIGHT_MIN, WEIGHT_MAX]``.
    """
    role_w = ROLE_WEIGHT.get(str(role).strip(), WEIGHT_NEUTRAL)
    priority_n = PRIORITY_NUDGE.get(str(priority).strip(), PRIORITY_NUDGE_DEFAULT)
    raw = role_w * priority_n
    return max(WEIGHT_MIN, min(WEIGHT_MAX, raw))


# ─────────────────────────────────────────────────────────────────────────────
# Module-level import with fallback so the patch target exists for tests.
# The try/except handles both the in-package path (app.db) and the root path.
try:
    from app.db import run_kv_get
except ModuleNotFoundError:
    try:
        from backend.app.db import run_kv_get
    except ModuleNotFoundError:
        run_kv_get = None  # type: ignore[assignment]

# ─────────────────────────────────────────────────────────────────────────────
# Canonical role / priority string values (must match frontend storage)
# ─────────────────────────────────────────────────────────────────────────────

ROLE_PRIMARY       = "system_of_record"
ROLE_SUPPORTING    = "operational_signal_source"
ROLE_SUPPLEMENTARY = "supplementary"

PRIORITY_HIGH   = "primary"
PRIORITY_MEDIUM = "secondary"
PRIORITY_LOW    = "tertiary"


# ─────────────────────────────────────────────────────────────────────────────
# Per-system weighting dataclass
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SystemWeighting:
    """Per-system role and priority as set by the user in Stack Builder.

    Attributes
    ----------
    system_id:
        The system identifier, e.g. ``'salesforce'``, ``'servicenow'``.
    role:
        User-assigned role. One of ``system_of_record``,
        ``operational_signal_source``, ``supplementary``, or an empty
        string when not set.
    priority:
        User-assigned priority. One of ``primary``, ``secondary``,
        ``tertiary``, or an empty string when not set.
    workflow_focus:
        Optional list of workflow area keys the user selected for this
        system, e.g. ``['intake_requests', 'approvals']``.
    confirmed:
        True when the user explicitly confirmed this weighting on the
        Stack Builder confirmation screen.
    """

    system_id: str
    role: str = ""
    priority: str = ""
    workflow_focus: List[str] = field(default_factory=list)
    confirmed: bool = False

    @property
    def is_primary_system(self) -> bool:
        """True when this system was marked as the primary (system of record)."""
        return self.priority == PRIORITY_HIGH

    @property
    def is_supporting_system(self) -> bool:
        """True when this system was marked as an operational signal source."""
        return self.role == ROLE_SUPPORTING

    @property
    def is_neutral(self) -> bool:
        """True when no meaningful weighting was recorded for this system."""
        return not self.role and not self.priority

    @property
    def base_role_weight(self) -> float:
        """R16-C1 T4: the role authority weight WITHOUT the priority nudge.

        Returns the raw ROLE_WEIGHT for this system's role, ignoring whatever
        priority the customer assigned.  Used by the provenance guard to enforce
        the observed-beats-inferred ordering: inferred evidence is capped at this
        value so the customer's priority preference cannot boost inferred patterns
        above directly-observed evidence from the same source.

        Returns :data:`WEIGHT_NEUTRAL` (1.0) for neutral/unconfigured systems.
        """
        if self.is_neutral:
            return WEIGHT_NEUTRAL
        return ROLE_WEIGHT.get(str(self.role).strip(), WEIGHT_NEUTRAL)

    @property
    def source_weight(self) -> float:
        """R16-C1 T2: compute the deterministic source weight for this system.

        Returns :data:`WEIGHT_NEUTRAL` (1.0) for neutral/unconfigured systems
        so no modulation is applied (backward compat for runs without context).
        """
        if self.is_neutral:
            return WEIGHT_NEUTRAL
        return compute_source_weight(self.role, self.priority)


# Sentinel neutral weighting returned when a system has no configuration.
_NEUTRAL = SystemWeighting(system_id="__neutral__")


# ─────────────────────────────────────────────────────────────────────────────
# Run-scoped weighting context
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class StackBuilderWeightingContext:
    """All per-system weightings for a single discovery run.

    Build via :func:`load_for_run` (reads from the run KV store) or via
    :meth:`neutral` (no-op fallback for older runs).

    Attributes
    ----------
    weightings:
        Mapping of system_id → :class:`SystemWeighting` for every
        configured system. Includes ALL selected systems for the run,
        not only the primary platform.
    selected_system_ids:
        All system IDs the user selected in Stack Builder for this run.
    pack_id:
        The pack this run executes against (informational).
    run_id:
        The run this context was loaded for (informational).
    is_neutral:
        True when no ``setup_context`` was found for the run. The scorer
        and corroboration engine treat this as a no-op (backward compat).
    """

    weightings: Dict[str, SystemWeighting] = field(default_factory=dict)
    selected_system_ids: List[str] = field(default_factory=list)
    pack_id: str = ""
    run_id: str = ""
    is_neutral: bool = False

    # ── Lookup ────────────────────────────────────────────────────────────────

    def get(self, system_id: str) -> SystemWeighting:
        """Return the weighting for *system_id*.

        Returns the neutral sentinel (``is_neutral=True``) when the system
        has no weighting configured — the caller can safely read ``role``
        and ``priority`` without a ``None`` check.
        """
        return self.weightings.get(system_id, _NEUTRAL)

    def has_weighting_for(self, system_id: str) -> bool:
        """True when an explicit, non-neutral weighting exists for *system_id*."""
        w = self.weightings.get(system_id)
        return w is not None and not w.is_neutral

    def get_source_weight(self, system_id: str) -> float:
        """R16-C1 T2: return the bounded source weight for *system_id*.

        Returns :data:`WEIGHT_NEUTRAL` (1.0) when:
        - the context is neutral (no Stack Builder setup for this run), or
        - the system has no configured weighting.

        This ensures no modulation occurs for unconfigured or neutral runs.
        """
        if self.is_neutral:
            return WEIGHT_NEUTRAL
        return self.get(system_id).source_weight

    # ── Factory helpers ───────────────────────────────────────────────────────

    @classmethod
    def neutral(cls) -> "StackBuilderWeightingContext":
        """Return an empty, no-op context.

        Used as the backward-compatible fallback for runs that pre-date
        Stack Builder weighting (no ``setup_context`` KV entry).
        """
        return cls(is_neutral=True)

    def to_debug_dict(self) -> Dict[str, Any]:
        """Compact debug representation for score_debug / audit logging.

        Includes the computed ``source_weight`` per system so any consumer
        can see exactly what multiplier was applied (R16-C1 T2 transparency).
        """
        return {
            "run_id":               self.run_id,
            "pack_id":              self.pack_id,
            "is_neutral":           self.is_neutral,
            "selected_system_ids":  self.selected_system_ids,
            "system_weightings": {
                sid: {
                    "role":          w.role,
                    "priority":      w.priority,
                    "confirmed":     w.confirmed,
                    "source_weight": w.source_weight,
                }
                for sid, w in self.weightings.items()
            },
        }


# ─────────────────────────────────────────────────────────────────────────────
# Parser helper
# ─────────────────────────────────────────────────────────────────────────────

def _parse_system_weighting(system_id: str, raw: Any) -> SystemWeighting:
    """Build a :class:`SystemWeighting` from a raw dict in setup_context.weightings."""
    if not isinstance(raw, dict):
        return SystemWeighting(system_id=system_id)

    focus = raw.get("workflowFocus") or []
    if isinstance(focus, str):
        focus = [focus]

    return SystemWeighting(
        system_id=system_id,
        role=str(raw.get("role") or ""),
        priority=str(raw.get("priority") or ""),
        workflow_focus=list(focus),
        confirmed=bool(raw.get("confirmed", False)),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Public loader — the only entry point for the pipeline
# ─────────────────────────────────────────────────────────────────────────────

def load_for_run(run_id: str) -> StackBuilderWeightingContext:
    """Load weighting context from a run's persisted ``setup_context`` KV entry.

    Returns :meth:`StackBuilderWeightingContext.neutral` when:

    * ``run_id`` is missing or empty.
    * No ``setup_context`` entry exists (older runs that pre-date R16-C1).
    * The stored context carries no ``weightings`` key.
    * Any read or parse error occurs.

    Never raises — all failures return neutral behavior so the discovery
    pipeline always has a valid context object.

    Parameters
    ----------
    run_id:
        The run identifier used to look up the ``setup_context:{run_id}``
        KV entry written by the Stack Builder launch endpoint.
    """
    if not run_id:
        return StackBuilderWeightingContext.neutral()

    try:
        if run_kv_get is None:
            return StackBuilderWeightingContext.neutral()

        ctx = run_kv_get("setup_context", run_id)
        if not isinstance(ctx, dict):
            logger.debug(
                "R16-C1: no setup_context for run_id=%s — using neutral weighting",
                run_id,
            )
            return StackBuilderWeightingContext.neutral()

        raw_weightings: Dict[str, Any] = ctx.get("weightings") or {}
        selected_ids: List[str] = ctx.get("selected_system_ids") or []
        pack_id: str = str(ctx.get("pack_id") or "")

        weightings: Dict[str, SystemWeighting] = {}
        for sid, raw in raw_weightings.items():
            sid = str(sid).strip()
            if sid:
                weightings[sid] = _parse_system_weighting(sid, raw)

        context = StackBuilderWeightingContext(
            weightings=weightings,
            selected_system_ids=[str(s) for s in selected_ids if s],
            pack_id=pack_id,
            run_id=run_id,
            is_neutral=not weightings,
        )

        if weightings:
            logger.info(
                "R16-C1: loaded weighting context for run_id=%s — %d systems configured: %s",
                run_id,
                len(weightings),
                ", ".join(
                    f"{sid}(role={w.role},priority={w.priority})"
                    for sid, w in weightings.items()
                ),
            )
        else:
            logger.debug(
                "R16-C1: setup_context present for run_id=%s but weightings empty — neutral",
                run_id,
            )

        return context

    except Exception as exc:  # noqa: BLE001 — never break the pipeline
        logger.warning(
            "R16-C1: failed to load weighting context for run_id=%s (non-blocking): %s",
            run_id,
            exc,
        )
        return StackBuilderWeightingContext.neutral()
