/**
 * SystemCard — SB-1 Sprint 7
 *
 * System selection card used in all group grids on Screen 2.
 * Shows: logo initials, system name, category tag, connection status dot,
 * and optional recommendation reason.
 *
 * Connection status dot:
 *   connected      → emerald dot
 *   needs_auth     → amber dot
 *   not_configured → muted/slate dot
 *
 * Recommendation reason:
 *   Shown as small green text under the category tag when present.
 *   Format: "Recommended for [reason]"
 *
 * States: default, hover, selected.
 */

import React from 'react';
import { SystemCard as SystemCardType, ConnectionStatus } from '../../types/stack_builder';

interface Props {
  system: SystemCardType;
  selected: boolean;
  recommendationReason?: string;
  onToggle: (id: string) => void;
}

function ConnectionDot({ status }: { status: ConnectionStatus }) {
  const classes: Record<ConnectionStatus, string> = {
    connected: 'bg-emerald-500',
    needs_auth: 'bg-amber-400',
    not_configured: 'bg-slate-600',
  };
  const labels: Record<ConnectionStatus, string> = {
    connected: 'Connected',
    needs_auth: 'Credentials needed',
    not_configured: 'Not configured',
  };
  return (
    <div
      className={`h-2 w-2 rounded-full flex-shrink-0 ${classes[status]}`}
      title={labels[status]}
      aria-label={labels[status]}
    />
  );
}

export default function SystemCard({ system, selected, recommendationReason, onToggle }: Props) {
  const handleKey = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      onToggle(system.id);
    }
  };

  return (
    <div
      role="checkbox"
      aria-checked={selected}
      tabIndex={0}
      onClick={() => onToggle(system.id)}
      onKeyDown={handleKey}
      className={[
        'relative cursor-pointer rounded-lg border p-3 transition-colors',
        'focus:outline-none focus:ring-2 focus:ring-accent/50',
        selected
          ? 'border-accent bg-panel2'
          : recommendationReason
          ? 'border-accent/30 bg-panel hover:border-accent/60'
          : 'border-border bg-panel hover:border-border/80',
      ].join(' ')}
    >
      {/* Connection status dot — top right */}
      <div className="absolute top-2 right-2">
        <ConnectionDot status={system.connectionStatus} />
      </div>

      {/* Logo */}
      <div className={`mb-2 h-8 w-8 rounded-lg ${system.logoColor} flex items-center justify-center`}>
        <span className="text-xs font-semibold text-text/80">{system.logoInitials}</span>
      </div>

      {/* Name */}
      <div className={`text-sm font-medium leading-tight mb-0.5 ${selected ? 'text-text' : 'text-text'}`}>
        {system.name}
      </div>

      {/* Category tag */}
      <div className="text-xs text-muted">{system.category}</div>

      {/* Recommendation reason */}
      {recommendationReason && !selected && (
        <div className="mt-1.5 text-xs text-accent font-medium">
          {recommendationReason}
        </div>
      )}
    </div>
  );
}
