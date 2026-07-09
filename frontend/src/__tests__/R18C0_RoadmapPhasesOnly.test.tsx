/**
 * R18-C0 P6 — Roadmap cleanup: phases only (AC6)
 *
 * AC6 (Testable): "The Roadmap page shows no 'Required Data Permissions' block
 * and displays phases only."
 *
 * The customer-facing Agent Roadmap must focus on rollout phases and the
 * opportunities assigned to each phase. The "Required Data Permissions" and
 * "Dependencies" blocks were removed from the stage card so the roadmap tells
 * the implementation story rather than acting as a permissions debugger.
 * (Permission readiness still lives on the Agentforce Blueprint — out of scope
 * here.)
 *
 * Run:
 *   npx vitest run src/__tests__/R18C0_RoadmapPhasesOnly.test.tsx
 */

import React from 'react';
import { render, screen, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { vi, describe, it, expect, beforeEach } from 'vitest';

const mockOpenReview = vi.fn();

vi.mock('../context/AnalystReviewContext', () => ({
  useAnalystReviewContext: () => ({
    opportunities: [], selectedId: null, select: vi.fn(),
    audit: [], setDecision: vi.fn(), saveOverride: vi.fn(),
    loading: false, error: null, refetch: vi.fn(),
  }),
}));

vi.mock('../context/ConnectorContext', () => ({
  useConnectorContext: () => ({
    all: [{ id: 'salesforce', name: 'Salesforce', status: 'connected' }],
  }),
}));

vi.mock('../components/pilot_roadmap/ReadinessPill', () => ({
  default: ({ status }: any) => <span>{status}</span>,
}));

import StageCard from '../components/pilot_roadmap/StageCard';
import StagesGrid from '../components/pilot_roadmap/StagesGrid';

// ── Fixtures — a stage that still CARRIES permission + dependency data ─────────
// The underlying roadmap data remains available; the point of AC6 is that it is
// NOT rendered on the roadmap surface.

const makeOpp = (id: string, title: string) => ({
  id, title, category: 'Approval Automation', tier: 'Quick Win' as const,
  impact: 7, effort: 3, confidence: 'HIGH' as const, decision: 'APPROVED' as const,
  aiRationale: '', evidenceIds: [],
  override: { isLocked: false, rationaleOverride: '', overrideReason: '', updatedAt: null },
  permissions: [], requiredPermissions: [],
});

const makeStageWithPerms = (id: 'NEXT_30' | 'NEXT_60' | 'NEXT_90') => ({
  id,
  title: `Stage ${id}`,
  summary: '',
  opportunities: [makeOpp('opp_001', 'Automate approvals')],
  requiredPermissions: [
    { label: 'Salesforce: read ProcessInstance', required: true, satisfied: false },
  ],
  dependencies: [
    { id: 'dep_1', label: 'ServiceNow incident sync', status: 'PENDING' as const },
  ],
});

const STAGE_WITH_PERMS = makeStageWithPerms('NEXT_30');

const STAGES_ALL = [
  makeStageWithPerms('NEXT_30'),
  makeStageWithPerms('NEXT_60'),
  makeStageWithPerms('NEXT_90'),
];

function renderCard(stage: any) {
  return render(
    <MemoryRouter>
      <StageCard stage={stage} onOpenReview={mockOpenReview} />
    </MemoryRouter>,
  );
}

describe('R18-C0 P6 — Roadmap stage card shows phases only (AC6)', () => {
  beforeEach(() => vi.clearAllMocks());

  it('does NOT render the "Required Data Permissions" block', () => {
    renderCard(STAGE_WITH_PERMS);
    expect(screen.queryByText('Required Data Permissions')).toBeNull();
    // The permission label carried in the data must not leak into the roadmap.
    expect(screen.queryByText('Salesforce: read ProcessInstance')).toBeNull();
  });

  it('does NOT render the "Dependencies" block', () => {
    renderCard(STAGE_WITH_PERMS);
    expect(screen.queryByText('Dependencies')).toBeNull();
    expect(screen.queryByText('ServiceNow incident sync')).toBeNull();
  });

  it('still renders Stage Readiness and Selected Opportunities', () => {
    renderCard(STAGE_WITH_PERMS);
    expect(screen.getByText('Stage Readiness')).toBeTruthy();
    expect(screen.getByText('Selected Opportunities')).toBeTruthy();
    expect(screen.getByTestId('opp-row-opp_001')).toBeTruthy();
    expect(within(screen.getByTestId('opp-row-opp_001')).getByText('Automate approvals')).toBeTruthy();
  });
});

describe('R18-C0 P6 — StagesGrid renders phase headings only (AC6)', () => {
  beforeEach(() => vi.clearAllMocks());

  it('renders Phase 1/2/3 headings and no permissions/dependencies blocks', () => {
    render(
      <MemoryRouter>
        <StagesGrid stages={STAGES_ALL} onOpenReview={mockOpenReview} />
      </MemoryRouter>,
    );
    expect(screen.getByTestId('phase-heading-NEXT_30').textContent).toContain('Phase 1');
    expect(screen.getByTestId('phase-heading-NEXT_60').textContent).toContain('Phase 2');
    expect(screen.getByTestId('phase-heading-NEXT_90').textContent).toContain('Phase 3');
    expect(screen.queryByText('Required Data Permissions')).toBeNull();
    expect(screen.queryByText('Dependencies')).toBeNull();
  });
});
