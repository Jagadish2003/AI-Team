/**
 * StackBuilderProgressBar — SB-1 Sprint 7
 *
 * Step indicator for the 4-screen setup flow.
 * Three states: active (accent fill), completed (green check), needs_attention (amber !), pending (muted).
 * Used on all four screens with the same component.
 *
 * Props:
 *   steps — array of ProgressStep with status per step
 */

import React from 'react';
import { ProgressStep, StepStatus } from '../../types/stack_builder';

interface Props {
  steps: ProgressStep[];
}

function StepDot({ step }: { step: ProgressStep }) {
  const base = 'flex h-6 w-6 flex-shrink-0 items-center justify-center rounded-full text-xs font-medium';

  if (step.status === 'completed') {
    return (
      <div className={`${base} bg-emerald-600/20 border border-emerald-500/40 text-emerald-400`}
        aria-label={`Step ${step.number} completed`}>
        <svg width="12" height="12" viewBox="0 0 12 12" fill="none" aria-hidden="true">
          <path d="M2 6l3 3 5-5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/>
        </svg>
      </div>
    );
  }

  if (step.status === 'active') {
    return (
      <div className={`${base} bg-accent text-white`}
        aria-label={`Step ${step.number} active`}>
        {step.number}
      </div>
    );
  }

  if (step.status === 'needs_attention') {
    return (
      <div className={`${base} bg-amber-500/20 border border-amber-500/40 text-amber-400`}
        aria-label={`Step ${step.number} needs attention`}>
        <svg width="12" height="12" viewBox="0 0 12 12" fill="none" aria-hidden="true">
          <path d="M6 4v3M6 8.5v.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"/>
        </svg>
      </div>
    );
  }

  // pending
  return (
    <div className={`${base} border border-border text-muted`}
      aria-label={`Step ${step.number} pending`}>
      {step.number}
    </div>
  );
}

function stepLabelColor(status: StepStatus): string {
  if (status === 'completed') return 'text-emerald-400';
  if (status === 'active') return 'text-accent font-medium';
  if (status === 'needs_attention') return 'text-amber-400';
  return 'text-muted';
}

function ConnectorLine({ fromStatus }: { fromStatus: StepStatus }) {
  const done = fromStatus === 'completed';
  return (
    <div className={`h-px flex-1 ${done ? 'bg-emerald-500/30' : 'bg-border'}`} aria-hidden="true" />
  );
}

export default function StackBuilderProgressBar({ steps }: Props) {
  return (
    <nav aria-label="Setup progress" className="flex items-center gap-2 mb-8">
      {steps.map((step, i) => (
        <React.Fragment key={step.number}>
          <div className="flex items-center gap-2">
            <StepDot step={step} />
            <span className={`text-xs ${stepLabelColor(step.status)}`}>
              {step.label}
            </span>
          </div>
          {i < steps.length - 1 && (
            <ConnectorLine fromStatus={step.status} />
          )}
        </React.Fragment>
      ))}
    </nav>
  );
}
