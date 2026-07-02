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

#: Microsoft Teams connector IDs (R17-A1 / AT-433). Teams is a conversation source
#: with the SAME MEDIUM ceiling discipline as Slack. Same maintenance obligation as
#: SLACK_SYSTEM_IDS: any new Teams connector ID must be added here or it would
#: silently bypass the ceiling.
TEAMS_SYSTEM_IDS: frozenset = frozenset({
    "teams",
    "teams_workspace",
})

#: All conversation sources (Slack + Teams). A finding scored from any of these
#: can never reach HIGH from the scorer alone — they are noisy, supplementary
#: signal and only corroborate findings carried by a system of record.
CONVERSATION_SOURCE_IDS: frozenset = SLACK_SYSTEM_IDS | TEAMS_SYSTEM_IDS

#: Source-label substrings that identify a conversation source (Slack / Teams) in
#: corroboration_sources lists. Compared case-insensitively.
_SLACK_SOURCE_LABEL = "slack"
_TEAMS_SOURCE_LABEL = "teams"
_CONVERSATION_SOURCE_LABELS = (_SLACK_SOURCE_LABEL, _TEAMS_SOURCE_LABEL)


def _is_conversation_source_label(label: str) -> bool:
    """True when a corroboration-source label denotes a conversation source."""
    low = label.lower()
    return any(cl in low for cl in _CONVERSATION_SOURCE_LABELS)


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

    # ── Rule 2: conversation-source system-ID always caps at MEDIUM ───────────
    # Slack and Teams are supplementary conversation sources; even if a customer
    # incorrectly assigns a system_of_record role to one, the system-ID clamp
    # enforces the ceiling.
    sid = system_id.strip().lower() if system_id else ""
    if sid in CONVERSATION_SOURCE_IDS:
        return CONFIDENCE_MEDIUM

    # ── Rule 3: conversation-source-only corroboration cannot reach HIGH ──────
    # When the only corroborating evidence is conversation sources (Slack and/or
    # Teams) with no primary system-of-record corroborator, confidence stays
    # MEDIUM. Reinforces COR-05: conversation escalation alone never elevates.
    if corroboration_sources is not None:
        has_conversation = any(
            _is_conversation_source_label(s) for s in corroboration_sources
        )
        has_primary = any(
            not _is_conversation_source_label(s) for s in corroboration_sources
        )
        if has_conversation and not has_primary:
            return CONFIDENCE_MEDIUM

    # ── Rule 4: Single-source cannot self-corroborate to HIGH ────────────────
    if is_single_source:
        return CONFIDENCE_MEDIUM

    return confidence


# ─────────────────────────────────────────────────────────────────────────────
# Convenience: corroboration-source-based Slack-only check
# ─────────────────────────────────────────────────────────────────────────────

def is_conversation_only_corroboration(corroboration_sources: List[str]) -> bool:
    """Return True when ``corroboration_sources`` contains only conversation-source
    labels — i.e. Slack and/or Teams — and no system-of-record label.

    Used to apply the conversation-source ceiling (MEDIUM) defensively: a list with
    any non-conversation label present returns False (a primary corroborator
    exists → COR-06 applies and HIGH is permissible). An empty list returns False
    (no corroboration at all is not conversation-only).

    Covers BOTH Slack and Teams (the two conversation sources) via
    :data:`_CONVERSATION_SOURCE_LABELS` — a Teams-only finding is clamped exactly
    like a Slack-only one. (Previously named ``is_slack_only_corroboration``; the
    Slack-only name masked that it must also gate Teams — see M1.)
    """
    if not corroboration_sources:
        return False

    def _is_conversation(label: str) -> bool:
        low = label.lower()
        return any(cl in low for cl in _CONVERSATION_SOURCE_LABELS)

    has_conversation = any(_is_conversation(s) for s in corroboration_sources)
    has_non_conversation = any(not _is_conversation(s) for s in corroboration_sources)
    return has_conversation and not has_non_conversation


#: Backwards-compatible alias. The ceiling applies to all conversation sources
#: (Slack + Teams), not Slack alone; prefer ``is_conversation_only_corroboration``.
is_slack_only_corroboration = is_conversation_only_corroboration
