/**
 * PillTag — SB-1 Sprint 7
 *
 * Reusable pill tag component used across all 4 screens:
 *   - Industry selector (Screen 1) — single select
 *   - Template selector (Screen 1) — single select
 *   - Salesforce cloud picker (Screen 2) — multi select
 *   - Workflow focus tags (Screen 3) — multi select, max 3
 *   - Role selector (Screen 3) — single select
 *   - Priority selector (Screen 3) — single select
 *
 * ARCHITECTURAL NOTE (Sprint 7 — confirmed by architect review May 2026):
 * PillTag is intentionally broad in Sprint 7 to cover all stack builder
 * pill use cases from a single component. This is acceptable while the
 * surface area is contained within the stack builder.
 *
 * Sprint 8 story: split into three focused variants:
 *   - SelectionPill  — for user-driven selection (focus, role, priority, systems)
 *   - StatusPill     — for read-only state display (confidence, connection status)
 *   - SummaryPill    — for compact summary chips (Screen 4 selected systems strip)
 *
 * Do NOT extend PillTag for non-stack-builder use cases in the existing app.
 * Use existing Badge.tsx for connector status in Integration Hub.
 *
 * Props:
 *   label     — display text
 *   selected  — current selected state
 *   disabled  — greyed out, not clickable (used for workflow focus at max 3)
 *   onToggle  — callback
 *   size      — 'sm' (default) or 'md' for role/priority selectors
 */

import React from 'react';

interface Props {
  label: string;
  selected: boolean;
  disabled?: boolean;
  onToggle: () => void;
  size?: 'sm' | 'md';
}

export default function PillTag({ label, selected, disabled = false, onToggle, size = 'sm' }: Props) {
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
    ? 'border-accent bg-accent/15 text-accent cursor-pointer'
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
