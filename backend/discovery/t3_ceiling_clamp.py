"""
R16-C1 T3 — Weighting Ceiling Clamp

Enforces the hard corroboration rules that weighting cannot override
(R16-C1 Section 2, "Hard Rules — Inviolable").

Three hard ceilings are enforced here:

  1. Supporting-only role ceiling
     A system configured with role "supporting", "operational_signal_source",
     or "supplementary" cannot single-handedly produce HIGH confidence from
     the scorer, regardless of its priority setting. Even a supporting source
     at primary priority (weight 0.66) must stay at MEDIUM from the scorer
     alone. Cross-system corroboration (corroboration_engine.py) may still
     elevate the final confidence to HIGH — the T3 clamp only prevents the
     single-source scorer verdict from being HIGH.

  2. Slack-only corroboration ceiling
     Slack is a supplementary signal source by definition. If the only
     corroborating evidence is Slack (COR-05 fires, no primary corroborator),
     confidence stays at MEDIUM. COR-06 (Slack + primary corroborator)
     is still allowed to reach HIGH. This is a hardcoded enterprise principle
     — see corroboration_rules.py COR-05.

  3. Single-source cap
     When only one system is connected, no self-corroboration is possible.
     The confidence ceiling is MEDIUM (COR-08). This is already enforced by
     the corroboration engine but is reinforced here as defense-in-depth.

Design principle (R16-C1 Section 6):
    "A customer must never be able to weight their way to a false
    HIGH-confidence finding."

This module is the single, authoritative enforcement point for those
inviolable ceilings. It is called:
  • by scorer.score() — immediately after _compute_confidence() returns, to
    clamp the single-source scorer verdict.
  • by corroboration_engine.apply_corroboration_confidence() — to guard the
    corroboration-elevated verdict against future rules that might accidentally
    violate the Slack / single-source ceiling.

Never import scoring or DB logic from here — this module must remain a pure
function usable from both the discovery layer and the app layer.
"""
from __future__ import annotations

from typing import List, Optional

# ─────────────────────────────────────────────────────────────────────────────
# Confidence level constants (mirror corroboration_rules.py — no cross-import)
# ─────────────────────────────────────────────────────────────────────────────

CONFIDENCE_HIGH   = "HIGH"
CONFIDENCE_MEDIUM = "MEDIUM"
CONFIDENCE_LOW    = "LOW"

# ─────────────────────────────────────────────────────────────────────────────
# Hard-rule sets (R16-C1 Section 2)
# ─────────────────────────────────────────────────────────────────────────────

#: Roles whose single-source evidence cannot produce HIGH from the scorer.
#: These map to all current "supporting" role strings in the stack builder.
SUPPORTING_ONLY_ROLES: frozenset = frozenset({
    "supporting",
    "operational_signal_source",   # current-app alias
    "supplementary",               # current-app alias
})

#: System identifiers that are always treated as supplementary/Slack sources.
#: A system with one of these IDs can never produce HIGH from the scorer alone,
#: regardless of the role configured in Stack Builder.
#:
#: MAINTENANCE OBLIGATION (mirrors the _COMMON_WORDS pattern in
#: hallucination_guard.py): this is the EXHAUSTIVE set of Slack connector IDs.
#: The ceiling is enforced by exact membership, so any new Slack connector
#: registered under a different ID (e.g. "slack_enterprise", "slack_grid",
#: "slack_connect") would silently BYPASS the Slack MEDIUM ceiling until it is
#: added here. When a new Slack connector ID is introduced, add it to this set
#: in the same change — otherwise a misconfigured (or simply newer) Slack
#: integration could manufacture a false HIGH-confidence finding.
SLACK_SYSTEM_IDS: frozenset = frozenset({
    "slack",
    "slack_workspace",
})

#: Source-label substrings that identify Slack in corroboration_sources lists.
#: Compared case-insensitively.
_SLACK_SOURCE_LABEL = "slack"


# ─────────────────────────────────────────────────────────────────────────────
# Public clamp function
# ─────────────────────────────────────────────────────────────────────────────

def apply_t3_ceiling_clamp(
    confidence: str,
    *,
    role: str = "",
    system_id: str = "",
    corroboration_sources: Optional[List[str]] = None,
    is_single_source: bool = False,
) -> str:
    """Enforce hard corroboration ceilings that weighting cannot override.

    This is the authoritative T3 enforcement function for R16-C1. Callers
    pass in the computed confidence and the context needed to apply each
    ceiling; this function returns the clamped confidence.

    Only HIGH confidence is clamped — MEDIUM and LOW are never raised or
    lowered by this function (it is a ceiling, not a floor).

    Hard rules applied (in order):

    1. **Supporting-only role** — ``role`` in SUPPORTING_ONLY_ROLES → MEDIUM.
       A supporting source cannot single-handedly produce HIGH, even at
       primary priority. This prevents weight-inflated supporting sources
       from manufacturing a false HIGH-confidence finding.

    2. **Slack system identity** — ``system_id`` in SLACK_SYSTEM_IDS → MEDIUM.
       Slack is always supplementary. Even if a customer incorrectly assigns a
       system_of_record role to a Slack integration, the system-ID-based clamp
       enforces the ceiling.

    3. **Slack-only corroboration** — ``corroboration_sources`` contains only
       Slack labels (no primary corroborator) → MEDIUM. Reinforces COR-05:
       Slack escalation alone never elevates to HIGH.

    4. **Single-source** — ``is_single_source=True`` → MEDIUM. Reinforces
       COR-08: a lone system cannot self-corroborate to HIGH.

    Parameters
    ----------
    confidence:
        The confidence level computed by the scorer or corroboration engine.
        One of ``"HIGH"``, ``"MEDIUM"``, ``"LOW"``.
    role:
        The configured Stack Builder role for the signal source, e.g.
        ``"supporting"``, ``"operational_signal_source"``. Empty string when
        the source has no configured role (neutral → no clamp applied).
    system_id:
        The system identifier (e.g. ``"slack"``). Checked independently of
        ``role`` so misconfigured roles cannot bypass the Slack ceiling.
    corroboration_sources:
        The list of human-readable corroboration source labels from
        ``CorroborationResult.corroboration_sources``. When provided, a
        Slack-only list forces MEDIUM. Pass ``None`` to skip this check.
    is_single_source:
        ``True`` when only one system is connected for this run (COR-08).

    Returns
    -------
    str
        The clamped confidence. Never higher than the applicable ceiling.
        Returns ``confidence`` unchanged when none of the hard rules apply.
    """
    # Only HIGH can be clamped — MEDIUM/LOW pass through unchanged.
    if confidence != CONFIDENCE_HIGH:
        return confidence

    # ── Rule 1: Supporting-only role cannot produce HIGH ──────────────────────
    role_key = role.strip() if role else ""
    if role_key in SUPPORTING_ONLY_ROLES:
        return CONFIDENCE_MEDIUM

    # ── Rule 2: Slack system-ID always caps at MEDIUM ─────────────────────────
    sid = system_id.strip().lower() if system_id else ""
    if sid in SLACK_SYSTEM_IDS:
        return CONFIDENCE_MEDIUM

    # ── Rule 3: Slack-only corroboration cannot reach HIGH ────────────────────
    if corroboration_sources is not None:
        has_slack = any(
            _SLACK_SOURCE_LABEL in s.lower() for s in corroboration_sources
        )
        has_non_slack = any(
            _SLACK_SOURCE_LABEL not in s.lower() for s in corroboration_sources
        )
        # Slack sources present, no non-Slack primary corroborator → MEDIUM
        if has_slack and not has_non_slack:
            return CONFIDENCE_MEDIUM

    # ── Rule 4: Single-source cannot self-corroborate to HIGH ────────────────
    if is_single_source:
        return CONFIDENCE_MEDIUM

    return confidence


# ─────────────────────────────────────────────────────────────────────────────
# Convenience: corroboration-source-based Slack-only check
# ─────────────────────────────────────────────────────────────────────────────

def is_slack_only_corroboration(corroboration_sources: List[str]) -> bool:
    """Return True when ``corroboration_sources`` contains only Slack labels.

    Used by the corroboration engine to apply the Slack ceiling defensively.
    A list with any non-Slack label present returns False (primary corroborator
    exists → COR-06 applies and HIGH is permissible).

    An empty list returns False (no corroboration at all is not Slack-only).
    """
    if not corroboration_sources:
        return False
    has_slack = any(_SLACK_SOURCE_LABEL in s.lower() for s in corroboration_sources)
    has_non_slack = any(_SLACK_SOURCE_LABEL not in s.lower() for s in corroboration_sources)
    return has_slack and not has_non_slack
