/**
 * StackBuilderProgressBar - SB-2 Sprint 7
 *
 * Four-step setup progress indicator for the guided discovery stack builder.
 */

import React from 'react';
import { ProgressStep, StepStatus } from '../../types/stack_builder';

interface Props {
  steps: ProgressStep[];
}

function StepDot({ step }: { step: ProgressStep }) {
  const base =
    'flex h-6 w-6 flex-shrink-0 items-center justify-center rounded-full text-xs font-medium transition-colors duration-200';

  if (step.status === 'completed') {
    return (
      <div
        className={`${base} bg-emerald-500/15 border border-emerald-500/50 text-emerald-500`}
        aria-label={`Step ${step.number}: ${step.label} — completed`}
      >
        <svg width="11" height="11" viewBox="0 0 11 11" fill="none" aria-hidden="true">
          <path
            d="M1.5 5.5L4 8l5-5"
            stroke="currentColor"
            strokeWidth="1.5"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
      </div>
    );
  }

  if (step.status === 'active') {
    return (
      <div
        className={`${base} bg-emerald-500 text-white`}
        aria-label={`Step ${step.number}: ${step.label} — current step`}
        aria-current="step"
      >
        {step.number}
      </div>
    );
  }

  if (step.status === 'needs_attention') {
    return (
      <div
        className={`${base} bg-amber-500/15 border border-amber-500/50 text-amber-500`}
        aria-label={`Step ${step.number}: ${step.label} — needs attention`}
      >
        <svg width="11" height="11" viewBox="0 0 11 11" fill="none" aria-hidden="true">
          <path
            d="M5.5 3.5v3"
            stroke="currentColor"
            strokeWidth="1.5"
            strokeLinecap="round"
          />
          <circle cx="5.5" cy="8" r="0.75" fill="currentColor" />
        </svg>
      </div>
    );
  }

  return (
    <div
      className={`${base} border border-border text-muted`}
      aria-label={`Step ${step.number}: ${step.label} — not yet reached`}
    >
      {step.number}
    </div>
  );
}

function stepLabelClass(status: StepStatus): string {
  switch (status) {
    case 'completed':
      return 'text-emerald-500';
    case 'active':
      return 'text-emerald-500 font-medium';
    case 'needs_attention':
      return 'text-amber-500';
    case 'pending':
    default:
      return 'text-muted';
  }
}

function ConnectorLine({ fromStatus }: { fromStatus: StepStatus }) {
  const isCompleted = fromStatus === 'completed';

  return (
    <div
      className={`h-px flex-1 transition-colors duration-200 ${
        isCompleted ? 'bg-emerald-500/40' : 'bg-border'
      }`}
      aria-hidden="true"
    />
  );
}

export default function StackBuilderProgressBar({ steps }: Props) {
  return (
    <nav aria-label="Setup progress" className="flex items-center gap-2 mb-8">
      {steps.map((step, i) => (
        <React.Fragment key={step.number}>
          <div className="flex flex-shrink-0 items-center gap-2">
            <StepDot step={step} />
            <span className={`text-xs whitespace-nowrap ${stepLabelClass(step.status)}`}>
              {step.label}
            </span>
          </div>
          {i < steps.length - 1 && <ConnectorLine fromStatus={step.status} />}
        </React.Fragment>
      ))}
    </nav>
  );
}
