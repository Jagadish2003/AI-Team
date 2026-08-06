// @vitest-environment jsdom
import React from 'react';
import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import type { OpportunityCandidate, ReviewAuditEvent } from '../types/analystReview';
import type { InterventionProjection } from '../types/enrichment';
import type { BlueprintResponse } from '../utils/blueprintTypes';

vi.mock('../context/RunContext', () => ({
  useRunContext: () => ({ runId: null }),
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
      description:
        'The projection assumes the agent handles the cases represented by this finding.',
    },
    {
      id: 'adoption_complete_for_cases',
      label: 'Adoption is complete for those cases',
      description: 'The projection applies after the identified cases use the agent path.',
    },
    {
      id: 'upstream_volume_within_observed_range',
      label: 'Upstream volume remains within its observed range',
      description: 'Incoming volume stays comparable to the observed baseline.',
    },
    {
      id: 'residual_requires_human_judgement',
      label: 'Residual cases still require human judgement',
      description: 'Exceptions and ambiguous cases still need human review.',
    },
    {
      id: 'limited_to_signal_and_horizon',
      label: 'Projection applies only to the measured signal and horizon shown',
      description: 'Projection is limited to reassignment hops over 30 days.',
    },
  ],
  affectedSignals: [],
  basis: {
    detectorId: 'HANDOFF_FRICTION',
    observedInstances: 240,
    observedPopulation: 800,
    instanceSignal: 'owner_changes_90d',
    populationSignal: 'total_cases_90d',
    baselineMean: 201.6,
    baselineStddev: 2.7,
    baselineWindowDays: 90,
    observedRunCount: 5,
    signalKey: 'service_cloud::HANDOFF_FRICTION::metric_value',
    confidence: 'HIGH',
    corroborationStatus: 'corroborated',
    corroborationSources: ['ServiceNow', 'Jira'],
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

describe('2.0-A1 T2 assumption ledger surfaces', () => {
  it('renders projection assumptions in Opportunity Review detail', () => {
    render(<OpportunityDetail opp={OPPORTUNITY} audit={AUDIT} />);

    expect(screen.getByTestId('projection-assumption-ledger')).toBeInTheDocument();
    expect(screen.getByText('Assumptions')).toBeInTheDocument();
    for (const assumption of PROJECTION.assumptionLedger) {
      expect(screen.getByText(assumption.label)).toBeInTheDocument();
    }
  });

  it('renders projection assumptions in Blueprint Details', () => {
    render(<BlueprintContent blueprint={BLUEPRINT} />);

    expect(screen.getByText('Projection Assumptions')).toBeInTheDocument();
    for (const assumption of PROJECTION.assumptionLedger) {
      expect(screen.getByText(assumption.label)).toBeInTheDocument();
    }
  });

  it('does not surface projection assumptions when the projection has no ledger', () => {
    const incompleteProjection = {
      ...PROJECTION,
      assumptionLedger: [],
    };

    render(
      <OpportunityDetail
        opp={{ ...OPPORTUNITY, projection: incompleteProjection }}
        audit={AUDIT}
      />,
    );

    expect(screen.queryByTestId('projection-assumption-ledger')).toBeNull();
  });
});
