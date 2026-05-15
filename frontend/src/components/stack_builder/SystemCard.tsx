/**
 * SystemCard — SB-4 Sprint 7
 *
 * System selection card used in all group grids on Screen 2 (Your Systems).
 * Renders in Group A (primary platforms), Group B (operational systems),
 * and Group C (data and engineering sources).
 *
 * Visual states:
 *   default      — border-border, bg-panel, muted category text
 *   hover        — border-emerald-500/40 (standard), border-emerald-500/50 (recommended)
 *   selected     — border-emerald-500, bg-emerald-500/[0.08]
 *   recommended  — border-emerald-500/25, recommendation reason in emerald below category
 *
 * Connection status dot (top-right corner):
 *   connected       — bg-emerald-500 (green)
 *   needs_auth      — bg-amber-400 (amber)
 *   not_configured  — bg-slate-300 (light grey — light background surface)
 *
 * Recommendation reason:
 *   Shows below the category tag when recommendationReason is provided.
 *   Visible in both selected AND unselected states — wireframe shows
 *   "Recommended for workflow signals" on selected Jira card.
 *   Colour: text-emerald-600 (selected), text-emerald-500 (unselected recommended).
 *
 * Token note:
 *   All selection and recommendation colours use the emerald/teal family.
 *   The accent token (#0D55D7, blue) is not used — consistent with all
 *   other selected states across the stack builder.
 *
 * tabIndex prop:
 *   Defaults to 0. Pass tabIndex={-1} for keyboard-focus management at the
 *   parent level. Sprint 8 story: roving focus within system groups.
 *
 * Accessibility:
 *   role="checkbox" — parent group should have role="group" and a group label.
 *   aria-checked reflects selection state.
 *   tabIndex prop — see above.
 *   Enter and Space toggle selection.
 *   ConnectionDot has aria-label for screen readers.
 *   Logo block is aria-hidden — decorative.
 *
 * Props:
 *   system               — SystemCard type from stack_builder types
 *   selected             — whether this system is currently selected
 *   recommendationReason — optional string, shown below category tag
 *   onToggle             — called with system.id when user toggles the card
 *   tabIndex             — optional, defaults to 0
 *
 * Usage:
 *   const { state, toggleSystem } = useSetupState();
 *   <SystemCard
 *     system={system}
 *     selected={state.selectedSystemIds.includes(system.id)}
 *     recommendationReason={getRecommendationReason(system.id, state.focusId)}
 *     onToggle={toggleSystem}
 *   />
 */

import React from 'react';
import { SystemCard as SystemCardType, ConnectionStatus } from '../../types/stack_builder';

interface Props {
  system: SystemCardType;
  selected: boolean;
  recommendationReason?: string;
  onToggle: (id: string) => void;
  selectionRole?: 'checkbox' | 'radio';
  /** Defaults to 0. Pass -1 for focus management at parent level (Sprint 8). */
  tabIndex?: number;
}

// ── Connection status dot ─────────────────────────────────────────────────────

function ConnectionDot({ status }: { status: ConnectionStatus }) {
  const classes: Record<ConnectionStatus, string> = {
    connected:      'bg-emerald-500',
    needs_auth:     'bg-amber-400',
    not_configured: 'bg-slate-300',
  };
  const labels: Record<ConnectionStatus, string> = {
    connected:      'Connected',
    needs_auth:     'Credentials needed',
    not_configured: 'Not yet configured',
  };
  return (
    <div
      className={`h-2 w-2 rounded-full flex-shrink-0 ${classes[status]}`}
      aria-label={labels[status]}
      role="img"
    />
  );
}

// ── SystemCard ────────────────────────────────────────────────────────────────

export default function SystemCard({
  system,
  selected,
  recommendationReason,
  onToggle,
  selectionRole = 'checkbox',
  tabIndex = 0,
}: Props) {
  const isRecommended = Boolean(recommendationReason);

  return (
    <button
      type="button"
      role={selectionRole}
      aria-checked={selected}
      tabIndex={tabIndex}
      onClick={() => onToggle(system.id)}
      className={[
        'relative w-full cursor-pointer rounded-lg border p-3 text-left transition-colors duration-150',
        'focus:outline-none focus:ring-2 focus:ring-accent/50',
        selected
          ? 'border-accent bg-accent/10 shadow-sm shadow-black/10'
          : isRecommended
          ? 'border-accent/30 bg-panel hover:border-accent/60 hover:bg-panel2'
          : 'border-border bg-panel hover:border-accent/50 hover:bg-panel2',
      ].filter(Boolean).join(' ')}
    >
      {/* Connection status dot — top right */}
      <div className="absolute top-2 right-2">
        <ConnectionDot status={system.connectionStatus} />
      </div>

      {/* Logo initials block — decorative */}
      <div
        className={`mb-2 h-8 w-8 rounded-lg ${system.logoColor} flex items-center justify-center`}
        aria-hidden="true"
      >
        <span className="text-xs font-semibold text-white/80">
          {system.logoInitials}
        </span>
      </div>

      {/* System name */}
      <div className="text-sm font-medium leading-tight mb-0.5 text-text">
        {system.name}
      </div>

      {/* Category tag */}
      <div className="text-xs text-muted">{system.category}</div>

      {/* Recommendation reason — visible in both selected and unselected states */}
      {isRecommended && (
        <div className={`mt-1.5 text-xs font-medium ${
          selected ? 'text-blue-100' : 'text-accent'
        }`}>
          {recommendationReason}
        </div>
      )}
    </button>
  );
}
