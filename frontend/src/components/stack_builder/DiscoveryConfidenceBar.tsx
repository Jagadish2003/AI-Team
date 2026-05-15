/**
 * DiscoveryConfidenceBar — SB-1 v1.1 Task 7 Sprint 7
 *
 * Persistent setup readiness indicator shown from Screen 2 onward.
 * Updates in real time as the user makes selections and confirms weightings.
 *
 * Three threshold states — all use the UI label "Discovery confidence":
 *
 *   basic  (0–40%)  — amber fill (bg-amber-400), amber label (text-amber-500)
 *                     neutral container (bg-panel border-border)
 *
 *   good   (40–75%) — amber-orange fill (bg-amber-500), amber label (text-amber-500)
 *                     neutral container (bg-panel border-border)
 *                     Wireframe Image 2: bar is clearly amber/orange — NOT blue.
 *                     The accent token (#0D55D7) was incorrect here.
 *
 *   strong (75–100%) — emerald fill (bg-emerald-500), emerald label (text-emerald-500)
 *                      subtle teal container (bg-emerald-500/[0.06] border-emerald-500/20)
 *
 * showSummary prop:
 *   false (default) — shows hint text only (Screens 2 and 3)
 *   true            — also shows state.summary sentence (Screen 4)
 *
 * Token note:
 *   good state uses bg-amber-500 (not accent blue). The amber family covers
 *   basic and good — basic is lighter (amber-400), good is richer (amber-500).
 *   strong state uses emerald-500 throughout — consistent with all other
 *   emerald elements in the stack builder.
 *   accent token (#0D55D7) is not used in this component.
 *
 * Accessibility:
 *   Track div has role="progressbar" with aria-valuenow, aria-valuemin,
 *   aria-valuemax, and aria-label.
 *   Container has no interactive role — purely informational.
 *
 * Props:
 *   state       — ConfidenceState: { level, fillPercent, hint, summary? }
 *   showSummary — show state.summary below hint (Screen 4 only)
 *
 * Usage:
 *   const { confidenceState } = useSetupState();
 *   <DiscoveryConfidenceBar state={confidenceState} />
 *   <DiscoveryConfidenceBar state={confidenceState} showSummary />
 */

import React from 'react';
import { ConfidenceState } from '../../types/stack_builder';

interface Props {
  state: ConfidenceState;
  showSummary?: boolean;
  className?: string;
}

export default function DiscoveryConfidenceBar({ state, showSummary = false, className = '' }: Props) {
  const trackFill: Record<string, string> = {
    basic:  'bg-amber-400',
    good:   'bg-amber-500',
    strong: 'bg-emerald-500',
  };

  const labelColor: Record<string, string> = {
    basic:  'text-amber-500',
    good:   'text-amber-500',
    strong: 'text-emerald-500',
  };

  const containerClass: Record<string, string> = {
    basic:  'bg-panel border-border',
    good:   'bg-panel border-border',
    strong: 'bg-emerald-500/[0.06] border-emerald-500/20',
  };

  const levelLabel: Record<string, string> = {
    basic:  'Basic',
    good:   'Good',
    strong: 'Strong',
  };

  return (
    <div className={`rounded-lg border px-4 py-3 ${containerClass[state.level]} ${className}`}>

      {/* Bar row */}
      <div className="flex items-center gap-3 mb-1.5">
        <span className="text-xs text-muted flex-shrink-0">Discovery confidence</span>

        {/* Track */}
        <div
          className="flex-1 h-1.5 rounded-full bg-border overflow-hidden"
          role="progressbar"
          aria-valuenow={state.fillPercent}
          aria-valuemin={0}
          aria-valuemax={100}
          aria-label={`Discovery confidence: ${levelLabel[state.level]}`}
        >
          <div
            className={`h-full rounded-full transition-all duration-500 ${trackFill[state.level]}`}
            style={{ width: `${state.fillPercent}%` }}
          />
        </div>

        <span className={`text-xs font-medium flex-shrink-0 ${labelColor[state.level]}`}>
          {levelLabel[state.level]}
        </span>
      </div>

      {/* Hint text */}
      <div className="text-xs text-muted leading-relaxed">
        {state.hint}
      </div>

      {/* Summary — Screen 4 only */}
      {showSummary && state.summary && (
        <div className={`mt-1.5 text-xs font-medium leading-relaxed ${labelColor[state.level]}`}>
          {state.summary}
        </div>
      )}
    </div>
  );
}
