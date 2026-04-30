import React from 'react';
import { fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import EvidenceCard from '../components/partial_results/EvidenceCard';
import OpportunityMatrix from '../components/opportunity_map/OpportunityMatrix';
import PartialResultsPage from '../pages/PartialResultsPage';
import { countOpportunitiesReferencing, deriveInterpretation } from '../utils/evidenceInterpreter';
import type { OpportunityCandidate } from '../types/analystReview';
import type { EvidenceReview } from '../types/partialResults';

const { mockNavigate, mockEvidence } = vi.hoisted(() => ({
  mockNavigate: vi.fn(),
  mockEvidence: {
    id: 'ev_001',
    tsLabel: '28 Apr 2026, 20:15',
    source: 'Salesforce',
    evidenceType: 'Metric' as const,
    title: 'Approval records exceeding SLA due to high delay in Discount Approval',
    snippet: '60 pending Discount Approval records with average delay of 21.33 days.',
    entities: ['ent_001'],
    confidence: 'MEDIUM' as const,
    decision: 'UNREVIEWED' as const,
  },
}));

vi.mock('react-router-dom', async (importOriginal) => {
  const actual = await importOriginal<typeof import('react-router-dom')>();
  return { ...actual, useNavigate: () => mockNavigate };
});

vi.mock('../components/common/TopNav', () => ({
  default: () => <nav data-testid="top-nav" />,
}));

vi.mock('../components/common/Toast', () => ({
  useToast: () => ({ push: vi.fn() }),
}));

vi.mock('../context/RunContext', () => ({
  useRunContext: () => ({ runId: 'RUN_001' }),
}));

vi.mock('../context/AnalystReviewContext', () => ({
  useAnalystReviewContext: () => ({
    opportunities: [
      {
        id: 'opp_001',
        title: 'Opportunity opp_001',
        category: 'Automation',
        tier: 'Quick Win',
        impact: 7,
        effort: 3,
        confidence: 'HIGH',
        aiRationale: 'Test rationale',
        evidenceIds: ['ev_001'],
        decision: 'UNREVIEWED',
        override: { isLocked: false, rationaleOverride: '', overrideReason: '', updatedAt: null },
      },
      {
        id: 'opp_002',
        title: 'Opportunity opp_002',
        category: 'Automation',
        tier: 'Quick Win',
        impact: 6,
        effort: 4,
        confidence: 'HIGH',
        aiRationale: 'Test rationale',
        evidenceIds: ['ev_001'],
        decision: 'UNREVIEWED',
        override: { isLocked: false, rationaleOverride: '', overrideReason: '', updatedAt: null },
      },
    ],
  }),
}));

vi.mock('../context/PartialResultsContext', () => ({
  usePartialResultsContext: () => ({
    filteredEntities: [],
    countsByType: { Application: 0, Workflow: 0, Service: 0, Role: 0, DataObject: 0, Other: 0 },
    entityTypes: { Application: true, Workflow: true, Service: true, Role: false, DataObject: false, Other: false },
    setEntityTypeEnabled: vi.fn(),
    queryEntities: '',
    setQueryEntities: vi.fn(),
    selectedEntityIds: [],
    toggleEntity: vi.fn(),
    clearSelection: vi.fn(),
    filteredEvidence: [mockEvidence],
    selectedEvidenceId: 'ev_001',
    selectEvidence: vi.fn(),
    sources: ['All Sources', 'Salesforce'],
    sourceFilter: 'All Sources',
    setSourceFilter: vi.fn(),
    queryEvidence: '',
    setQueryEvidence: vi.fn(),
    selectedEvidence: mockEvidence,
    approveSelected: vi.fn(),
    rejectSelected: vi.fn(),
    saveDraftEnabled: true,
    setSaveDraftEnabled: vi.fn(),
    goPrev: vi.fn(),
    goNext: vi.fn(),
    canPrev: false,
    canNext: false,
    positionLabel: '1 of 1',
    loading: false,
    error: null,
    refetch: vi.fn(),
  }),
}));

const EV_APPROVAL = mockEvidence as EvidenceReview;

const EV_HANDOFF: EvidenceReview = {
  ...EV_APPROVAL,
  id: 'ev_002',
  title: 'Elevated case owner reassignment rate detected',
  snippet: '492 owner changes recorded across 300 Cases.',
};

const EV_KNOWLEDGE: EvidenceReview = {
  ...EV_APPROVAL,
  id: 'ev_003',
  title: 'Significant knowledge gap in Case resolution - low KB article linkage',
  snippet: '36 Cases resolved without KB linkage.',
};

const EV_ECHO: EvidenceReview = {
  ...EV_APPROVAL,
  id: 'ev_004',
  source: 'ServiceNow',
  evidenceType: 'Log',
  title: 'Cross-system ticket duplication detected across Salesforce and external systems',
  snippet: '350 ServiceNow incidents reference Salesforce case IDs.',
  confidence: 'HIGH',
  decision: 'APPROVED',
};

function makeOpportunity(
  id: string,
  evidenceIds: string[],
  impact: number,
  effort: number,
  decision: OpportunityCandidate['decision'] = 'UNREVIEWED',
): OpportunityCandidate {
  return {
    id,
    title: `Opportunity ${id}`,
    category: 'Automation',
    tier: 'Quick Win',
    impact,
    effort,
    confidence: 'HIGH',
    aiRationale: 'Test rationale',
    evidenceIds,
    decision,
    override: { isLocked: false, rationaleOverride: '', overrideReason: '', updatedAt: null },
    permissions: [],
    requiredPermissions: [],
  };
}

function renderCard(
  evidence: EvidenceReview,
  opportunities: OpportunityCandidate[] = [],
  onSelect = vi.fn(),
) {
  return render(
    <MemoryRouter>
      <EvidenceCard
        evidence={evidence}
        selected={false}
        onSelect={onSelect}
        opportunities={opportunities}
      />
    </MemoryRouter>,
  );
}

function renderMatrix(opportunities: OpportunityCandidate[]) {
  return render(
    <MemoryRouter>
      <OpportunityMatrix filtered={opportunities} selectedId={null} onSelect={vi.fn()} />
    </MemoryRouter>,
  );
}

describe('evidence interpreter', () => {
  it('returns scoped interpretations for known Sprint 4 evidence patterns', () => {
    expect(deriveInterpretation(EV_APPROVAL).toLowerCase()).toContain('bottleneck');
    expect(deriveInterpretation(EV_HANDOFF).toLowerCase()).toContain('routing');
    expect(deriveInterpretation(EV_KNOWLEDGE).toLowerCase()).toContain('knowledge');
    expect(deriveInterpretation(EV_ECHO).toLowerCase()).toContain('duplication');
  });

  it('counts opportunity links defensively', () => {
    expect(countOpportunitiesReferencing('ev_001', [{ evidenceIds: ['ev_001'] }, {}])).toBe(1);
    expect(countOpportunitiesReferencing('ev_999', [{ evidenceIds: ['ev_001'] }])).toBe(0);
  });
});

describe('EvidenceCard', () => {
  beforeEach(() => {
    mockNavigate.mockClear();
  });

  it('uses a div role button for the card so the linkage button is not nested inside another button', () => {
    const opportunities = [makeOpportunity('opp_001', ['ev_001'], 7, 3)];
    renderCard(EV_APPROVAL, opportunities);
    const card = screen.getByTestId('evidence-card-ev_001');

    expect(card.tagName).toBe('DIV');
    expect(card).toHaveAttribute('role', 'button');
    expect(card.querySelectorAll('button')).toHaveLength(1);
  });

  it('selects the card with Enter and Space', () => {
    const onSelect = vi.fn();
    renderCard(EV_APPROVAL, [], onSelect);
    const card = screen.getByTestId('evidence-card-ev_001');

    fireEvent.keyDown(card, { key: 'Enter' });
    fireEvent.keyDown(card, { key: ' ' });

    expect(onSelect).toHaveBeenCalledTimes(2);
    expect(onSelect).toHaveBeenCalledWith('ev_001');
  });

  it('shows source, interpretation, and linked opportunity count', () => {
    renderCard(EV_APPROVAL, [
      makeOpportunity('opp_001', ['ev_001'], 7, 3),
      makeOpportunity('opp_002', ['ev_001'], 6, 4),
    ]);

    expect(screen.getByText('Salesforce')).toBeInTheDocument();
    expect(screen.getByText('What this means')).toBeInTheDocument();
    expect(screen.getByText(/Referenced by 2 opportunities/)).toBeInTheDocument();
  });

  it('shows the not-linked message when no opportunities reference the evidence', () => {
    renderCard(EV_APPROVAL, []);
    expect(screen.getByText(/Not yet linked/)).toBeInTheDocument();
  });

  it('navigates linkage to Opportunity Review with oppId, not evidenceId', () => {
    renderCard(EV_APPROVAL, [makeOpportunity('opp_001', ['ev_001'], 7, 3)]);

    fireEvent.click(screen.getByTestId('evidence-card-linkage-ev_001'));

    expect(mockNavigate).toHaveBeenCalledWith('/opportunity-review?oppId=opp_001');
    expect(mockNavigate.mock.calls[0][0]).not.toContain('evidenceId');
  });
});

describe('OpportunityMatrix', () => {
  it('uses FOUNDATION instead of LOW HANGING FRUIT', () => {
    renderMatrix([makeOpportunity('opp_001', [], 7, 3)]);

    expect(screen.getByText('FOUNDATION')).toBeInTheDocument();
    expect(screen.queryByText('LOW HANGING FRUIT')).not.toBeInTheDocument();
  });

  it('warns when multiple opportunities collapse onto a shared impact or effort score', () => {
    renderMatrix([
      makeOpportunity('opp_001', [], 5, 2),
      makeOpportunity('opp_002', [], 5, 7),
    ]);

    expect(screen.getByTestId('score-collapse-warning')).toBeInTheDocument();
  });

  it('does not warn when opportunities have distinct impact and effort scores', () => {
    renderMatrix([
      makeOpportunity('opp_001', [], 5, 2),
      makeOpportunity('opp_002', [], 8, 7),
    ]);

    expect(screen.queryByTestId('score-collapse-warning')).not.toBeInTheDocument();
  });

  it('renders approved bubbles green and rejected bubbles red', () => {
    renderMatrix([
      makeOpportunity('opp_001', [], 5, 2, 'APPROVED'),
      makeOpportunity('opp_002', [], 8, 7, 'REJECTED'),
    ]);

    const circles = document.querySelectorAll('circle');
    expect(circles[0]).toHaveAttribute('fill', 'rgba(180,60,60,0.35)');
    expect(circles[1]).toHaveAttribute('fill', 'rgba(0,180,120,0.35)');
  });
});

describe('PartialResultsPage', () => {
  it('shows Evidence Collection as the page title', () => {
    render(
      <MemoryRouter>
        <PartialResultsPage />
      </MemoryRouter>,
    );

    expect(screen.getByTestId('page-title')).toHaveTextContent('Evidence Collection');
    expect(screen.queryByText('Partial Results')).not.toBeInTheDocument();
  });
});
