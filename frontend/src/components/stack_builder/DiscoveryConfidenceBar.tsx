/**
 * DiscoveryConfidenceBar — SB-1 Sprint 7
 *
 * Persistent confidence indicator shown from Screen 2 onward.
 * Updates as user makes selections and confirms source weighting.
 *
 * Three threshold states:
 *   basic   (0-40%)   — amber fill, amber label
 *   good    (40-75%)  — blue fill, blue label
 *   strong  (75-100%) — green fill, green label
 *
 * Props:
 *   state — ConfidenceState with level, fillPercent, hint, and optional summary
 *   showSummary — true on Screen 4 to show the "why this setup is strong" sentence
 */

import React from 'react';
import { ConfidenceState } from '../../types/stack_builder';

interface Props {
  state: ConfidenceState;
  showSummary?: boolean;
}

export default function DiscoveryConfidenceBar({ state, showSummary = false }: Props) {
  const trackFill: Record<string, string> = {
    basic: 'bg-amber-400',
    good: 'bg-accent',
    strong: 'bg-emerald-500',
  };

  const labelColor: Record<string, string> = {
    basic: 'text-amber-400',
    good: 'text-accent',
    strong: 'text-emerald-400',
  };

  const containerBg: Record<string, string> = {
    basic: 'bg-panel border-border',
    good: 'bg-panel border-border',
    strong: 'bg-emerald-900/20 border-emerald-500/20',
  };

  const levelLabel: Record<string, string> = {
    basic: 'Basic',
    good: 'Good',
    strong: 'Strong',
  };

  return (
    <div className={`rounded-lg border px-4 py-3 mb-6 ${containerBg[state.level]}`}>
      <div className="flex items-center gap-3 mb-1.5">
        {state.level === 'strong' && (
          <svg className="text-emerald-400 flex-shrink-0" width="14" height="14" viewBox="0 0 14 14" fill="none" aria-hidden="true">
            <circle cx="7" cy="7" r="6" stroke="currentColor" strokeWidth="1.2"/>
            <path d="M4.5 7l2 2 3-3" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" strokeLinejoin="round"/>
          </svg>
        )}
        <span className="text-xs text-muted flex-shrink-0">Discovery confidence</span>

        {/* Track */}
        <div className="flex-1 h-1 rounded-full bg-border overflow-hidden" role="progressbar"
          aria-valuenow={state.fillPercent} aria-valuemin={0} aria-valuemax={100}
          aria-label={`Discovery confidence: ${levelLabel[state.level]}`}>
          <div
            className={`h-full rounded-full transition-all duration-500 ${trackFill[state.level]}`}
            style={{ width: `${state.fillPercent}%` }}
          />
        </div>

        <span className={`text-xs font-medium flex-shrink-0 ${labelColor[state.level]}`}>
          {levelLabel[state.level]}
        </span>
      </div>

      {/* Actionable hint — always shown */}
      <div className="text-xs text-muted leading-relaxed">
        {state.hint}
      </div>

      {/* Summary sentence — Screen 4 only */}
      {showSummary && state.summary && (
        <div className={`mt-1.5 text-xs font-medium leading-relaxed ${labelColor[state.level]}`}>
          {state.summary}
        </div>
      )}
    </div>
  );
}
