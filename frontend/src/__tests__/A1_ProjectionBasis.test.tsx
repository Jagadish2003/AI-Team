// @vitest-environment jsdom
import React from 'react';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';
import type { OpportunityCandidate, ReviewAuditEvent } from '../types/analystReview';
import type { InterventionProjection } from '../types/enrichment';
import type { BlueprintResponse } from '../utils/blueprintTypes';

vi.mock('../context/RunContext', () => ({
  useRunContext: () => ({ runId: 'run_basis' }),
}));

vi.mock('../context/AnalystReviewContext', () => ({
  useAnalystReviewContext: () => ({ select: vi.fn() }),
}));

vi.mock('../api/enrichmentApi', () => ({
  fetchOppEnrichment: vi.fn().mockResolvedValue(null),
}));

// Release 2.0 Arc A UI (the projection surfaces this file tests) is hidden by
// default for the demo (see src/config/releaseFlags.ts). These tests exist to
// verify the underlying implementation still renders correctly when the flag
// is on, so they mock it true rather than asserting against hidden output.
vi.mock('../config/releaseFlags', () => ({
  showRelease2ArcAUi: true,
}));

import OpportunityDetail from '../components/analyst_review/OpportunityDetail';
import TopQuickWins from '../components/executive_report/TopQuickWins';
import { BlueprintContent } from '../pages/BlueprintPage';

const PROJECTION: InterventionProjection = {
  schemaVersion: '1.0.0',
  direction: 'improves',
  magnitudeBand: {
    lowPct: 25,
    highPct: 55,
    basisUnit: 'of the recurring instances',
    label: '25-55% of the recurring instances',
  },
  observationHorizonDays: 30,
  manualStepReplaced: 'manual reassignment review',
  movementSignal: {
    concept: 'reassignment_hops',
    conceptLabel: 'Reassignment hops',
    signalName: 'owner_changes_90d',
    unit: 'count',
    currentValue: 240,
    directionOfImprovement: 'decrease',
  },
  assumptionLedger: [
    {
      id: 'agent_handles_identified_cases',
      label: 'Agent handles the identified recurring cases',
      description: 'The projection assumes the agent handles the observed cases.',
    },
  ],
  affectedSignals: [],
  basis: {
    detectorId: 'HANDOFF_FRICTION',
    observedInstances: 240,
    observedPopulation: 800,
    observationWindowDays: 90,
    instanceSignal: 'owner_changes_90d',
    populationSignal: 'total_cases_90d',
    signalUsed: {
      signalName: 'owner_changes_90d',
      concept: 'reassignment_hops',
      conceptLabel: 'Reassignment hops',
      unit: 'count',
    },
    baselineValue: 201.6,
    baselineMean: 201.6,
    baselineStddev: 2.7,
    baselineWindowDays: 90,
    observedRunCount: 5,
    signalKey: 'service_cloud::HANDOFF_FRICTION::metric_value',
    confidence: 'HIGH',
    corroborationStatus: 'corroborated',
    corroborationSources: ['ServiceNow', 'Jira'],
    evidenceStrength: 'strong',
    thinEvidence: false,
    packId: 'service_cloud',
    packVersion: '1.2.0',
    evidenceIds: ['ev_001'],
  },
  bandWidthInputs: {
    sampleTier: 'strong',
    sampleSize: 800,
    recurrenceStability: 'steady',
    corroborationStatus: 'corroborated',
    thinEvidence: false,
  },
  confidenceCapped: false,
};

const THIN_PROJECTION: InterventionProjection = {
  ...PROJECTION,
  magnitudeBand: {
    ...PROJECTION.magnitudeBand!,
    lowPct: 5,
    highPct: 75,
    label: '5-75% of the recurring instances',
  },
  basis: {
    ...PROJECTION.basis,
    observedInstances: 6,
    observedPopulation: 12,
    evidenceStrength: 'thin',
    thinEvidence: true,
  },
  bandWidthInputs: {
    ...PROJECTION.bandWidthInputs,
    sampleTier: 'thin',
    sampleSize: 12,
    thinEvidence: true,
  },
};

const OPPORTUNITY: OpportunityCandidate = {
  id: 'opp_001',
  title: 'Reduce case routing friction',
  category: 'Ticket Routing',
  tier: 'Quick Win',
  impact: 8,
  effort: 3,
  confidence: 'HIGH',
  aiRationale: 'Owner changes are elevated.',
  evidenceIds: ['ev_001'],
  decision: 'UNREVIEWED',
  override: {
    isLocked: false,
    rationaleOverride: '',
    overrideReason: '',
    updatedAt: null,
  },
  projection: PROJECTION,
};

const AUDIT: ReviewAuditEvent[] = [];

const BLUEPRINT: BlueprintResponse = {
  oppId: 'opp_001',
  agentName: 'Case Routing Agent',
  agentTopic: 'Route recurring cases to the correct team.',
  agentTopicIsLlm: false,
  suggestedActions: [],
  guardrails: [],
  agentforcePermissions: [],
  complexity: {
    label: 'Standard Configuration',
    description: 'Standard agent configuration.',
    tier: 'Quick Win',
  },
  evidenceIds: ['ev_001'],
  detectorId: 'HANDOFF_FRICTION',
  projection: PROJECTION,
};

describe('2.0-A1 T3 projection computation basis surfaces', () => {
  it('renders full projection basis in Opportunity Review detail', () => {
    render(<OpportunityDetail opp={OPPORTUNITY} audit={AUDIT} />);

    const basis = screen.getByTestId('projection-basis-panel');
    expect(basis).toHaveTextContent('Projection Basis');
    expect(basis).toHaveTextContent('240 instances');
    expect(basis).toHaveTextContent('90 days');
    expect(basis).toHaveTextContent('201.6');
    expect(basis).toHaveTextContent('Reassignment hops');
    expect(basis).toHaveTextContent('owner_changes_90d');
    expect(basis).toHaveTextContent('Corroborated');
    expect(basis).toHaveTextContent('Strong Evidence');
  });

  it('keeps thin-evidence warning language in Projection Band, not duplicated in Basis', () => {
    render(
      <OpportunityDetail
        opp={{ ...OPPORTUNITY, projection: THIN_PROJECTION }}
        audit={AUDIT}
      />,
    );

    expect(screen.getByTestId('projection-basis-panel')).toHaveTextContent('Thin Evidence');
    expect(screen.getByTestId('projection-basis-panel')).not.toHaveTextContent(
      'projection band is wider because evidence is limited',
    );
    expect(screen.getByTestId('projection-band-rationale')).toHaveTextContent(
      'Evidence is limited',
    );
  });

  it('renders compact projection basis in Agentforce Blueprint near complexity context', () => {
    render(<BlueprintContent blueprint={BLUEPRINT} />);

    const basis = screen.getByTestId('projection-basis-compact');
    expect(screen.getByText('Projection Basis')).toBeInTheDocument();
    expect(basis).toHaveTextContent('240 observed instances');
    expect(basis).toHaveTextContent('90-day window');
    expect(basis).toHaveTextContent('baseline 201.6');
    expect(basis).toHaveTextContent('signal Reassignment hops');
    expect(basis).toHaveTextContent('corroborated');
    expect(basis).toHaveTextContent('strong evidence');
  });

  it('renders summary-level projection basis without the thin-evidence warning in Executive Report quick wins', () => {
    render(
      <MemoryRouter>
        <TopQuickWins
          quickWins={[{ ...OPPORTUNITY, projection: THIN_PROJECTION }]}
        />
      </MemoryRouter>,
    );

    const basis = screen.getByTestId('executive-report-projection-basis-opp_001');
    expect(basis).toHaveTextContent('Basis: 6 observed instances');
    expect(basis).toHaveTextContent('90-day window');
    expect(basis).toHaveTextContent('baseline 201.6');
    expect(basis).toHaveTextContent('signal Reassignment hops');
    expect(basis).toHaveTextContent('thin evidence');
    expect(screen.queryByText(/projection band is wider because evidence is limited/i))
      .not.toBeInTheDocument();
  });
});
