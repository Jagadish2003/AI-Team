// @vitest-environment jsdom
//
// 2.0-A1 T5 — recommendation copy on the screens the story names.
//
//   * Opportunity Review → AI Analysis / Suggested Next Steps
//   * Agentforce Blueprint → Agent Purpose
//   * Executive Report → Top Quick Wins
//
// Two things are pinned. First, the intervention-language recommendation is
// actually rendered on each surface, with all five parts. Second — and this is
// the one that matters — NO rendered surface carries guarantee or
// point-estimate savings language, swept over the rendered DOM text rather than
// over a fixture, so a component that composes its own sentence is caught.
import React from 'react';
import { render, screen, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';
import type { OpportunityCandidate, ReviewAuditEvent } from '../types/analystReview';
import type { InterventionProjection } from '../types/enrichment';
import type { BlueprintResponse } from '../utils/blueprintTypes';

vi.mock('../context/RunContext', () => ({
  useRunContext: () => ({ runId: 'run_rec' }),
}));

vi.mock('../context/AnalystReviewContext', () => ({
  useAnalystReviewContext: () => ({ select: vi.fn() }),
}));

vi.mock('../api/enrichmentApi', () => ({
  fetchOppEnrichment: vi.fn().mockResolvedValue(null),
}));

import OpportunityDetail from '../components/analyst_review/OpportunityDetail';
import TopQuickWins from '../components/executive_report/TopQuickWins';
import { BlueprintContent } from '../pages/BlueprintPage';
import {
  recommendationHeadline,
  recommendationNextSteps,
  recommendationSummary,
} from '../components/projection/ProjectionRecommendation';

// ---------------------------------------------------------------------------
// Prohibited vocabulary — the frontend mirror of the backend guard.
// ---------------------------------------------------------------------------

const PROHIBITED = [
  'will reduce',
  'will save',
  'will cut',
  'guarantee',
  'guaranteed',
  'savings',
  'roi',
  'eliminates',
  'eliminating',
  'ensures',
  'cost reduction',
  'payback',
];

function assertNoProhibitedCopy(text: string, where: string) {
  const lowered = text.toLowerCase();
  for (const phrase of PROHIBITED) {
    expect(lowered, `${where} contains prohibited phrase "${phrase}"`).not.toContain(
      phrase,
    );
  }
}

// ---------------------------------------------------------------------------
// Fixtures — the backend's real payload shape.
// ---------------------------------------------------------------------------

const RECOMMENDATION = {
  schemaVersion: '1.0.0',
  headline:
    'Agent handles the 240 recurring reassignment cases; the residual requires judgement (cases whose correct owner is genuinely ambiguous).',
  parts: [
    {
      id: 'agent_handles',
      label: 'What the agent handles',
      text: 'The agent takes over manually re-routing cases between queues to find the right owner.',
    },
    {
      id: 'cases_in_scope',
      label: 'Cases in scope',
      text: 'In scope: the 240 recurring reassignment cases, measured over the observed 90-day window.',
    },
    {
      id: 'remains_manual',
      label: 'What remains manual',
      text: 'Remaining manual: cases whose correct owner is genuinely ambiguous.',
    },
    {
      id: 'signal_expected_to_move',
      label: 'Signal expected to move',
      text: 'The signal expected to move is reassignment hops (owner_changes_90d), currently 240; a lower value is the expected direction of movement.',
    },
    {
      id: 'band_and_horizon',
      label: 'Projection band and horizon',
      text: 'Projected movement is a band of 23-57% of the recurring instances, observable over about 30 days.',
    },
  ],
  nextSteps: [
    'Confirm with the owning team that the 240 recurring reassignment cases match the pattern described here.',
    'Agree the boundary for cases whose correct owner is genuinely ambiguous, so the agent’s scope is explicit before build.',
    'Record the current value of owner_changes_90d as the baseline to re-measure against after the agent is live.',
  ],
  summary:
    'Agent handles the 240 recurring reassignment cases; the residual requires judgement (cases whose correct owner is genuinely ambiguous). The agent takes over manually re-routing cases between queues to find the right owner.',
};

const PROJECTION: InterventionProjection = {
  schemaVersion: '1.1.0',
  direction: 'improves',
  magnitudeBand: {
    lowPct: 23,
    highPct: 57,
    basisUnit: 'of the recurring instances',
    label: '23-57% of the recurring instances',
  },
  observationHorizonDays: 30,
  manualStepReplaced: 'manually re-routing cases between queues to find the right owner',
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
    evidenceLabel: 'Strong evidence',
    bandLabel: 'Moderate band',
    packId: 'service_cloud',
    packVersion: '1.2.0',
    evidenceIds: ['ev_001'],
  },
  bandWidthInputs: {
    sampleTier: 'strong',
    sampleSize: 800,
    recurrenceStability: 'steady',
    corroborationStatus: 'corroborated',
    confidenceCapped: false,
    thinEvidence: false,
  },
  recommendation: RECOMMENDATION,
  confidenceCapped: false,
};

const OPPORTUNITY: OpportunityCandidate = {
  id: 'opp_001',
  title: 'Elevated case reassignment',
  category: 'Ticket Routing',
  tier: 'Quick Win',
  impact: 8,
  effort: 3,
  confidence: 'HIGH',
  aiRationale: 'Owner changes are running above the handoff threshold.',
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
  agentTopic: `${RECOMMENDATION.headline} Owner changes are running above the handoff threshold.`,
  agentTopicIsLlm: false,
  suggestedActions: [
    {
      action: 'Analyse case attributes at creation',
      object: 'Case object',
      detail: 'Agent reads Case subject, type, and category to determine team assignment.',
    },
  ],
  guardrails: ['Agent must escalate any case type it has not previously handled.'],
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

// ---------------------------------------------------------------------------
// Opportunity Review — AI Analysis / Suggested Next Steps
// ---------------------------------------------------------------------------

describe('2.0-A1 T5 — Opportunity Review recommendation copy', () => {
  it('renders the intervention-language headline', () => {
    render(<OpportunityDetail opp={OPPORTUNITY} audit={AUDIT} />);

    const panel = screen.getByTestId('projection-recommendation-panel');
    expect(within(panel).getByTestId('recommendation-headline')).toHaveTextContent(
      'Agent handles the 240 recurring reassignment cases',
    );
    expect(within(panel).getByTestId('recommendation-headline')).toHaveTextContent(
      'the residual requires judgement',
    );
  });

  it('renders all five required parts of the recommendation', () => {
    render(<OpportunityDetail opp={OPPORTUNITY} audit={AUDIT} />);

    const panel = screen.getByTestId('projection-recommendation-panel');
    for (const partId of [
      'agent_handles',
      'cases_in_scope',
      'remains_manual',
      'signal_expected_to_move',
      'band_and_horizon',
    ]) {
      expect(
        within(panel).getByTestId(`recommendation-part-${partId}`),
      ).toBeInTheDocument();
    }
  });

  it('states what remains manual and which measured signal should move', () => {
    render(<OpportunityDetail opp={OPPORTUNITY} audit={AUDIT} />);

    const panel = screen.getByTestId('projection-recommendation-panel');
    expect(
      within(panel).getByTestId('recommendation-part-remains_manual'),
    ).toHaveTextContent('Remaining manual');
    expect(
      within(panel).getByTestId('recommendation-part-signal_expected_to_move'),
    ).toHaveTextContent('owner_changes_90d');
    expect(
      within(panel).getByTestId('recommendation-part-band_and_horizon'),
    ).toHaveTextContent('23-57%');
  });

  it('renders suggested next steps as actions', () => {
    render(<OpportunityDetail opp={OPPORTUNITY} audit={AUDIT} />);

    const steps = screen.getByTestId('recommendation-next-steps');
    expect(steps).toHaveTextContent('Confirm with the owning team');
    expect(steps).toHaveTextContent('baseline');
  });

  it('carries no guarantee or savings language anywhere in the rendered panel', () => {
    render(<OpportunityDetail opp={OPPORTUNITY} audit={AUDIT} />);

    assertNoProhibitedCopy(
      screen.getByTestId('projection-recommendation-panel').textContent ?? '',
      'Opportunity Review recommendation',
    );
  });

  it('renders fallback intervention copy when an older projection has no recommendation', () => {
    const withoutRecommendation = {
      ...OPPORTUNITY,
      projection: { ...PROJECTION, recommendation: null },
    };
    render(<OpportunityDetail opp={withoutRecommendation} audit={AUDIT} />);

    const panel = screen.getByTestId('projection-recommendation-panel');
    expect(panel).toHaveTextContent('Agent handles 240 recurring instances');
    expect(panel).toHaveTextContent('What remains manual');
  });
});

// ---------------------------------------------------------------------------
// Agentforce Blueprint — Agent Purpose
// ---------------------------------------------------------------------------

describe('2.0-A1 T5 — Blueprint Agent Purpose', () => {
  it('leads the purpose with the intervention statement', () => {
    render(<BlueprintContent blueprint={BLUEPRINT} />);

    const compact = screen.getByTestId('projection-recommendation-compact');
    expect(within(compact).getByTestId('recommendation-headline')).toHaveTextContent(
      'Agent handles the 240 recurring reassignment cases',
    );
  });

  it('names what remains manual beside the purpose', () => {
    render(<BlueprintContent blueprint={BLUEPRINT} />);

    expect(screen.getByTestId('projection-recommendation-compact')).toHaveTextContent(
      'Remaining manual',
    );
  });

  it('carries no guarantee or savings language in purpose or actions', () => {
    const { container } = render(<BlueprintContent blueprint={BLUEPRINT} />);
    assertNoProhibitedCopy(container.textContent ?? '', 'Blueprint');
  });
});

// ---------------------------------------------------------------------------
// Executive Report — Top Quick Wins
// ---------------------------------------------------------------------------

describe('2.0-A1 T5 — Executive Report Top Quick Wins', () => {
  it('states each quick win as an intervention', () => {
    render(
      <MemoryRouter>
        <TopQuickWins quickWins={[OPPORTUNITY]} />
      </MemoryRouter>,
    );

    expect(screen.getByTestId('recommendation-headline')).toHaveTextContent(
      'Agent handles the 240 recurring reassignment cases',
    );
  });

  it('carries no guarantee or savings language', () => {
    const { container } = render(
      <MemoryRouter>
        <TopQuickWins quickWins={[OPPORTUNITY]} />
      </MemoryRouter>,
    );
    assertNoProhibitedCopy(container.textContent ?? '', 'Top Quick Wins');
  });

  it('shows fallback intervention copy for a quick win with no recommendation', () => {
    render(
      <MemoryRouter>
        <TopQuickWins
          quickWins={[
            { ...OPPORTUNITY, projection: { ...PROJECTION, recommendation: null } },
          ]}
        />
      </MemoryRouter>,
    );

    expect(screen.getByTestId('recommendation-headline')).toHaveTextContent(
      'Agent handles 240 recurring instances',
    );
  });
});

// ---------------------------------------------------------------------------
// The accessors themselves — used by the PDF export, which has no DOM.
// ---------------------------------------------------------------------------

describe('2.0-A1 T5 — recommendation accessors', () => {
  it('exposes the headline, summary, and next steps', () => {
    expect(recommendationHeadline(PROJECTION)).toBe(RECOMMENDATION.headline);
    expect(recommendationSummary(PROJECTION)).toBe(RECOMMENDATION.summary);
    expect(recommendationNextSteps(PROJECTION)).toHaveLength(3);
  });

  it('returns null only when there is no projection to describe', () => {
    expect(recommendationHeadline(null)).toBeNull();
    expect(recommendationHeadline({ ...PROJECTION, recommendation: null })).toContain(
      'Agent handles 240 recurring instances',
    );
    expect(recommendationSummary(null)).toBeNull();
    expect(recommendationNextSteps(null)).toEqual([]);
  });

  it('composes a summary from the parts when the backend sent none', () => {
    const summary = recommendationSummary({
      ...PROJECTION,
      recommendation: { ...RECOMMENDATION, summary: '' },
    });
    expect(summary).toContain('Agent handles the 240 recurring reassignment cases');
    expect(summary).toContain('Remaining manual');
  });

  it('produces no prohibited copy from any accessor', () => {
    assertNoProhibitedCopy(recommendationSummary(PROJECTION) ?? '', 'summary');
    recommendationNextSteps(PROJECTION).forEach((step, index) =>
      assertNoProhibitedCopy(step, `nextSteps[${index}]`),
    );
  });
});
