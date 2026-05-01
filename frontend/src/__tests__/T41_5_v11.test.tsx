/**
 * T41-5 v1.1 — StageCard and StagesGrid tests
 *
 * Changes from v1.0:
 *   - AR-I1: Blueprint link is inside the opportunity row (card-bound)
 *     not in a detached list below the card.
 *   - AR-I1b: Multiple opportunities each have their own Blueprint link
 *     and each link navigates with the correct oppId.
 *   - AR-I1c: Blueprint link stopPropagation — clicking link does not
 *     also trigger onOpenReview.
 *   - All v1.0 tests preserved.
 */
import React from 'react';
import { render, screen, fireEvent, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { vi, describe, it, expect, beforeEach } from 'vitest';

const mockNavigate = vi.fn();
const mockSelect   = vi.fn();
const mockOpenReview = vi.fn();

vi.mock('react-router-dom', async (importOriginal) => {
  const actual = await importOriginal<typeof import('react-router-dom')>();
  return { ...actual, useNavigate: () => mockNavigate };
});

vi.mock('../context/AnalystReviewContext', () => ({
  useAnalystReviewContext: () => ({
    opportunities: [], selectedId: null, select: mockSelect,
    audit: [], setDecision: vi.fn(), saveOverride: vi.fn(),
    loading: false, error: null, refetch: vi.fn(),
  }),
}));

let mockSFConnected = false;
vi.mock('../context/ConnectorContext', () => ({
  useConnectorContext: () => ({
    all: mockSFConnected
      ? [{ id: 'salesforce', name: 'Salesforce', status: 'connected' }]
      : [{ id: 'servicenow', name: 'ServiceNow', status: 'connected' }],
  }),
}));

vi.mock('../components/pilot_roadmap/ReadinessPill', () => ({ default: ({ status }: any) => <span>{status}</span> }));

import StageCard from '../components/pilot_roadmap/StageCard';
import StagesGrid from '../components/pilot_roadmap/StagesGrid';

// ── Fixtures ──────────────────────────────────────────────────────────────────

const makeOpp = (id: string, title: string) => ({
  id, title, category: 'Approval Automation', tier: 'Quick Win' as const,
  impact: 7, effort: 3, confidence: 'HIGH' as const, decision: 'APPROVED' as const,
  aiRationale: '', evidenceIds: [],
  override: { isLocked: false, rationaleOverride: '', overrideReason: '', updatedAt: null },
  permissions: [], requiredPermissions: [],
});

const makeStage = (id: string, opps: any[]) => ({
  id, title: `Stage ${id}`, summary: '',
  opportunities: opps,
  requiredPermissions: [], dependencies: [],
});

const OPP_A = makeOpp('opp_001', 'Automate approvals');
const OPP_B = makeOpp('opp_002', 'Reduce case routing');
const STAGE_TWO_OPPS = makeStage('NEXT_30', [OPP_A, OPP_B]);
const STAGE_ONE_OPP  = makeStage('NEXT_30', [OPP_A]);
const STAGE_EMPTY    = makeStage('NEXT_30', []);

function renderCard(stage: any, renderBlueprintLink?: (id: string) => React.ReactNode) {
  return render(
    <MemoryRouter>
      <StageCard stage={stage} onOpenReview={mockOpenReview} renderBlueprintLink={renderBlueprintLink} />
    </MemoryRouter>,
  );
}

function renderGrid(stages: any[]) {
  return render(
    <MemoryRouter>
      <StagesGrid stages={stages} onOpenReview={mockOpenReview} />
    </MemoryRouter>,
  );
}

// ── StageCard tests ───────────────────────────────────────────────────────────

describe('StageCard — render prop integration', () => {

  beforeEach(() => { vi.clearAllMocks(); mockSFConnected = false; });

  it('renders opportunity rows', () => {
    renderCard(STAGE_TWO_OPPS);
    expect(screen.getByTestId('opp-row-opp_001')).toBeTruthy();
    expect(screen.getByTestId('opp-row-opp_002')).toBeTruthy();
  });

  it('renders without Blueprint links when no render prop provided', () => {
    renderCard(STAGE_TWO_OPPS);
    expect(screen.queryByTestId('blueprint-link-opp_001')).toBeNull();
    expect(screen.queryByTestId('blueprint-link-opp_002')).toBeNull();
  });

  it('AR-I1: Blueprint link renders INSIDE opp_001 row when render prop provided', () => {
    renderCard(STAGE_TWO_OPPS, (oppId) => (
      <button data-testid={`blueprint-link-${oppId}`}>Blueprint</button>
    ));
    const row = screen.getByTestId('opp-row-opp_001');
    // The link must be a descendant of the row — not detached
    expect(within(row).getByTestId('blueprint-link-opp_001')).toBeTruthy();
  });

  it('AR-I1: Blueprint link renders INSIDE opp_002 row independently', () => {
    renderCard(STAGE_TWO_OPPS, (oppId) => (
      <button data-testid={`blueprint-link-${oppId}`}>Blueprint</button>
    ));
    const row = screen.getByTestId('opp-row-opp_002');
    expect(within(row).getByTestId('blueprint-link-opp_002')).toBeTruthy();
  });

  it('AR-I1: link for opp_001 is NOT inside opp_002 row', () => {
    renderCard(STAGE_TWO_OPPS, (oppId) => (
      <button data-testid={`blueprint-link-${oppId}`}>Blueprint</button>
    ));
    const row002 = screen.getByTestId('opp-row-opp_002');
    // opp_001 link must not appear inside the opp_002 row
    expect(within(row002).queryByTestId('blueprint-link-opp_001')).toBeNull();
  });

  it('renders correctly with empty opportunities', () => {
    renderCard(STAGE_EMPTY);
    expect(screen.getByText('No opportunities assigned to this stage yet.')).toBeTruthy();
  });
});

// ── StagesGrid tests ──────────────────────────────────────────────────────────

describe('StagesGrid — T41-5 v1.1', () => {

  beforeEach(() => { vi.clearAllMocks(); mockSFConnected = false; });

  it('AR2: no 30/60/90 day language in headings', () => {
    renderGrid([STAGE_TWO_OPPS]);
    expect(screen.queryByText(/30 DAYS/i)).toBeNull();
  });

  it('AR3: Phase 1 heading present', () => {
    renderGrid([makeStage('NEXT_30', [OPP_A])]);
    expect(screen.getByTestId('phase-heading-NEXT_30').textContent).toContain('Phase 1');
  });

  it('AR4: Blueprint links visible when Salesforce connected', () => {
    mockSFConnected = true;
    renderGrid([STAGE_TWO_OPPS]);
    expect(screen.getByTestId('blueprint-link-opp_001')).toBeTruthy();
    expect(screen.getByTestId('blueprint-link-opp_002')).toBeTruthy();
  });

  it('AR5: Blueprint links absent when Salesforce not connected', () => {
    mockSFConnected = false;
    renderGrid([STAGE_TWO_OPPS]);
    expect(screen.queryByTestId('blueprint-link-opp_001')).toBeNull();
    expect(screen.queryByTestId('blueprint-link-opp_002')).toBeNull();
  });

  it('AR-I1b: each link navigates with its own correct oppId', () => {
    mockSFConnected = true;
    renderGrid([STAGE_TWO_OPPS]);
    fireEvent.click(screen.getByTestId('blueprint-link-opp_001'));
    expect(mockNavigate).toHaveBeenCalledWith(expect.stringContaining('oppId=opp_001'));
    fireEvent.click(screen.getByTestId('blueprint-link-opp_002'));
    expect(mockNavigate).toHaveBeenCalledWith(expect.stringContaining('oppId=opp_002'));
  });

  it('AR-I1c: clicking Blueprint link does not trigger onOpenReview', () => {
    mockSFConnected = true;
    renderGrid([STAGE_ONE_OPP]);
    fireEvent.click(screen.getByTestId('blueprint-link-opp_001'));
    expect(mockOpenReview).not.toHaveBeenCalled();
  });

  it('AR-I1: link is inside the opportunity row (card-bound)', () => {
    mockSFConnected = true;
    renderGrid([STAGE_TWO_OPPS]);
    const row = screen.getByTestId('opp-row-opp_001');
    expect(within(row).getByTestId('blueprint-link-opp_001')).toBeTruthy();
  });
});
