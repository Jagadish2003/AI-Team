/**
 * R18-C0 P8 — Re-editable review decisions (frontend, AC8)
 *
 * After a reviewer approves or rejects, the Approve/Reject buttons must stay
 * editable (for analyst+), so a prior choice can be corrected — e.g. flipping
 * Reject → Approve. The current decision stays visible via the button label and
 * fill state. Each change is sent to the backend, which appends a NEW audit
 * event preserving the prior one (covered by the backend contract test).
 *
 * Run:
 *   npx vitest run src/__tests__/R18C0_ReeditableDecisions.test.tsx
 */
import { render, screen } from '@testing-library/react';
import { describe, it, expect, vi } from 'vitest';

import type { OpportunityCandidate } from '../types/analystReview';
import type { Decision } from '../types/common';

const h: { role: 'owner' | 'analyst' | 'viewer' } = { role: 'analyst' };

vi.mock('../context/AuthContext', () => ({
  useAuthOptional: () => ({ user: { email: 'analyst@dwp.com', role: h.role } }),
}));

import ReasoningOverride from '../components/analyst_review/ReasoningOverride';

function makeOpp(decision: Decision): OpportunityCandidate {
  return {
    id: 'opp_001',
    title: 'Test opportunity',
    category: 'ops',
    tier: 'Quick Win',
    impact: 5,
    effort: 2,
    confidence: 'HIGH',
    aiRationale: 'because',
    evidenceIds: [],
    decision,
    override: { isLocked: false, rationaleOverride: '', overrideReason: '', updatedAt: null },
  };
}

function renderOverride(opp: OpportunityCandidate, onDecision = vi.fn()) {
  render(
    <ReasoningOverride
      opp={opp}
      audit={[]}
      onSave={vi.fn()}
      onViewEvidence={vi.fn()}
      onDecision={onDecision}
    />,
  );
  return { onDecision };
}

describe('R18-C0 P8 — decisions remain editable after a decision (AC8)', () => {
  it('keeps Approve and Reject enabled for an analyst after APPROVED', () => {
    h.role = 'analyst';
    renderOverride(makeOpp('APPROVED'));
    // Current decision shows as "Approved"; both controls stay usable.
    expect(screen.getByRole('button', { name: /approved/i })).toBeEnabled();
    expect(screen.getByRole('button', { name: /reject/i })).toBeEnabled();
  });

  it('keeps Approve and Reject enabled for an analyst after REJECTED', () => {
    h.role = 'analyst';
    renderOverride(makeOpp('REJECTED'));
    expect(screen.getByRole('button', { name: /approve/i })).toBeEnabled();
    expect(screen.getByRole('button', { name: /rejected/i })).toBeEnabled();
  });

  it('lets a reviewer flip REJECTED → APPROVED (fires onDecision again)', () => {
    h.role = 'analyst';
    const { onDecision } = renderOverride(makeOpp('REJECTED'));
    const approve = screen.getByRole('button', { name: /approve/i });
    expect(approve).toBeEnabled();
    approve.click();
    expect(onDecision).toHaveBeenCalledWith('APPROVED');
  });

  it('still disables decision controls for a viewer (RBAC preserved)', () => {
    h.role = 'viewer';
    renderOverride(makeOpp('APPROVED'));
    expect(screen.getByRole('button', { name: /approved/i })).toBeDisabled();
    expect(screen.getByRole('button', { name: /reject/i })).toBeDisabled();
  });
});
