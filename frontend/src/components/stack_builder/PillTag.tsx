/**
 * PillTag — SB-6 Sprint 7
 *
 * Reusable pill tag component used across all 4 screens:
 *   Screen 1 — Industry selector (single select)
 *   Screen 1 — Template selector (single select)
 *   Screen 2 — Salesforce cloud picker (multi select)
 *   Screen 3 — Role selector (single select)
 *   Screen 3 — Priority selector (single select)
 *   Screen 3 — Workflow focus tags (multi select, max 3)
 *
 * Visual states:
 *   selected  — border-emerald-500 bg-emerald-500/15 text-emerald-600 cursor-pointer
 *   default   — border-border bg-panel text-muted cursor-pointer
 *               hover: border-emerald-500/50 text-text
 *   disabled  — border-border/50 bg-panel/50 text-muted/50 cursor-not-allowed
 *               tabIndex=-1, not clickable, aria-disabled
 *
 * Token note:
 *   All interactive states use the emerald/teal family — consistent with
 *   FocusCard, SystemCard, SystemWeightingCard, and ProgressBar.
 *   The accent token (#0D55D7, blue) is not used in this component.
 *
 * Size variants:
 *   sm (default) — px-2.5 py-1 text-xs — industry/template/cloud/workflow pills
 *   md           — px-3 py-1.5 text-sm — role and priority selectors (Screen 3)
 *
 * Accessibility:
 *   <button type="button"> — keyboard focusable by default.
 *   role="checkbox" — reflects togglable selected state.
 *   aria-checked={selected}.
 *   aria-disabled={disabled} + disabled attr — prevents interaction.
 *   tabIndex=-1 when disabled — removed from tab order.
 *   Enter and Space activate toggle (when not disabled).
 *   focus:ring-2 focus:ring-emerald-500/50.
 *
 * ARCHITECTURAL NOTE (Sprint 7 — confirmed by architect review May 2026):
 * PillTag is intentionally broad in Sprint 7 to cover all stack builder
 * pill use cases from a single component. This is acceptable while the
 * surface area is contained within the stack builder.
 *
 * Sprint 8 story: split into three focused variants:
 *   SelectionPill — user-driven selection (focus, role, priority, systems)
 *   StatusPill    — read-only state display (confidence, connection status)
 *   SummaryPill   — compact summary chips (Screen 4 selected systems strip)
 *
 * Do NOT extend PillTag for non-stack-builder use cases in the existing app.
 * Use existing Badge.tsx for connector status in Integration Hub.
 *
 * Props:
 *   label    — display text
 *   selected — current selected state
 *   disabled — greyed out, not clickable (workflow focus at max 3)
 *   onToggle — called on click or Enter/Space when not disabled
 *   size     — 'sm' (default) or 'md'
 */

import React from 'react';

interface Props {
  label: string;
  selected: boolean;
  disabled?: boolean;
  onToggle: () => void;
  size?: 'sm' | 'md';
}

export default function PillTag({
  label,
  selected,
  disabled = false,
  onToggle,
  size = 'sm',
}: Props) {
  const handleKey = (e: React.KeyboardEvent) => {
    if (disabled) return;
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      onToggle();
    }
  };

  const sizeClasses = size === 'md'
    ? 'px-3 py-1.5 text-sm'
    : 'px-2.5 py-1 text-xs';

  const stateClasses = disabled
    ? 'border-border/50 bg-panel/50 text-muted/50 cursor-not-allowed'
    : selected
    ? 'border-accent bg-accent/15 text-blue-100 cursor-pointer'
    : 'border-border bg-panel text-muted hover:border-accent/50 hover:text-text cursor-pointer';

  return (
    <button
      type="button"
      role="checkbox"
      aria-checked={selected}
      aria-disabled={disabled}
      disabled={disabled}
      tabIndex={disabled ? -1 : 0}
      onClick={disabled ? undefined : onToggle}
      onKeyDown={handleKey}
      className={[
        'inline-flex items-center rounded-full border font-medium leading-none transition-colors',
        'focus:outline-none focus:ring-2 focus:ring-accent/50',
        sizeClasses,
        stateClasses,
      ].join(' ')}
    >
      {label}
    </button>
  );
}
