// @vitest-environment jsdom
//
// 2.0-A1 T4 — the band and its evidence label on the screens the story names.
//
//   * Opportunity Review — displays the resulting band and the evidence label;
//   * Agent Roadmap (inside the Agentforce Blueprint) — uses projection
//     strength CAREFULLY: it is displayed with its capped caveat, and a capped
//     (single-source) finding never presents above a corroborated equivalent
//     (AC4), while every other ordering decision is preserved.
//
// Every projection here is a literal fixture matching what the backend emits,
// so a failure names a rendering rule that broke rather than a fetch that drifted.
import React from 'react';
import { render, screen, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import type { OpportunityCandidate, ReviewAuditEvent } from '../types/analystReview';
import type { InterventionProjection } from '../types/enrichment';
import type { BlueprintResponse } from '../utils/blueprintTypes';
import type { RoadmapStage } from '../types/pilotRoadmap';

vi.mock('../context/RunContext', () => ({
  useRunContext: () => ({ runId: 'run_band' }),
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
import StageCard from '../components/pilot_roadmap/StageCard';
import { BlueprintContent } from '../pages/BlueprintPage';
import {
  demoteCappedProjections,
  orderByProjectionStrength,
  projectionRankKey,
} from '../components/projection/ProjectionBand';

// ---------------------------------------------------------------------------
// Fixtures — the backend's real payload shapes.
// ---------------------------------------------------------------------------

/** Strong evidence on all four axes: narrow-ish band, uncapped, high strength. */
const CORROBORATED: InterventionProjection = {
  schemaVersion: '1.1.0',
  direction: 'improves',
  magnitudeBand: {
    lowPct: 23,
    highPct: 57,
    basisUnit: 'of the recurring instances',
    label: '23-57% of the recurring instances',
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
    evidenceTier: 'strong',
    evidenceLabel: 'Strong evidence',
    bandTier: 'moderate',
    bandLabel: 'Moderate band',
    bandWidthRationale:
      'Moderate band: computed from 800 observed instances (strong sample), steady recurrence, corroborated corroboration - strong evidence.',
    bandWidthModelVersion: '1.0.0',
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
  bandWidth: {
    modelVersion: '1.0.0',
    lowPct: 23,
    highPct: 57,
    widthPct: 34,
    halfWidth: 0.1725,
    evidencePenalty: 0.075,
    evidenceQuality: 0.925,
    evidenceTier: 'strong',
    evidenceLabel: 'Strong evidence',
    bandTier: 'moderate',
    bandLabel: 'Moderate band',
    thinEvidence: false,
    confidenceCapped: false,
    rationale:
      'Moderate band: computed from 800 observed instances (strong sample), steady recurrence, corroborated corroboration - strong evidence.',
    drivers: [
      {
        axis: 'sample_size',
        label: 'Sample size',
        value: 'strong (800 observed)',
        penalty: 0,
        weight: 0.35,
        widensByPct: 0,
      },
      {
        axis: 'recurrence_stability',
        label: 'Recurrence stability',
        value: 'steady',
        penalty: 0,
        weight: 0.25,
        widensByPct: 0,
      },
      {
        axis: 'corroboration_status',
        label: 'Corroboration status',
        value: 'corroborated',
        penalty: 0.3,
        weight: 0.25,
        widensByPct: 4.5,
      },
      {
        axis: 'confidence_cap',
        label: 'Confidence cap status',
        value: 'not capped',
        penalty: 0,
        weight: 0.15,
        widensByPct: 0,
      },
    ],
    inputs: {
      sampleTier: 'strong',
      sampleSize: 800,
      recurrenceStability: 'steady',
      corroborationStatus: 'corroborated',
      confidenceCapped: false,
    },
  },
  projectionStrength: {
    value: 0.925,
    tier: 'strong',
    label: 'Strong projection strength',
    capped: false,
    cappedLabel: null,
    comparableWithCapped: true,
  },
  confidenceCapped: false,
};

/**
 * The AC4 case: the SAME finding with corroboration removed. Larger sample,
 * wider band, capped strength — the payload the backend actually emits.
 */
const CAPPED: InterventionProjection = {
  ...CORROBORATED,
  magnitudeBand: {
    lowPct: 13,
    highPct: 67,
    basisUnit: 'of the recurring instances',
    label: '13-67% of the recurring instances',
  },
  basis: {
    ...CORROBORATED.basis,
    corroborationStatus: 'single_source',
    corroborationSources: [],
    evidenceStrength: 'thin',
    thinEvidence: true,
    evidenceTier: 'adequate',
    evidenceLabel: 'Adequate evidence - band widened',
    bandTier: 'wide',
    bandLabel: 'Wide band',
  },
  bandWidthInputs: {
    ...CORROBORATED.bandWidthInputs,
    corroborationStatus: 'single_source',
    confidenceCapped: true,
    thinEvidence: true,
  },
  bandWidth: {
    ...CORROBORATED.bandWidth!,
    lowPct: 13,
    highPct: 67,
    widthPct: 54,
    evidencePenalty: 0.4,
    evidenceQuality: 0.6,
    evidenceTier: 'adequate',
    evidenceLabel: 'Adequate evidence - band widened',
    bandTier: 'wide',
    bandLabel: 'Wide band',
    thinEvidence: true,
    confidenceCapped: true,
    rationale:
      'Wide band: computed from 800 observed instances (strong sample), steady recurrence, single source corroboration, capped confidence - adequate evidence.',
    inputs: {
      ...CORROBORATED.bandWidth!.inputs,
      corroborationStatus: 'single_source',
      confidenceCapped: true,
    },
  },
  projectionStrength: {
    value: 0.5,
    tier: 'moderate',
    label: 'Capped - single-source confidence',
    capped: true,
    cappedLabel: 'Capped - single-source confidence',
    comparableWithCapped: false,
  },
  confidenceCapped: true,
};

/** A finding below the projection floor: no band, therefore no strength. */
const NO_BAND: InterventionProjection = {
  ...CORROBORATED,
  direction: 'no_material_change',
  magnitudeBand: null,
  bandWidth: null,
  projectionStrength: {
    value: null,
    tier: null,
    label: 'Not projected - evidence below the projection floor',
    capped: false,
    cappedLabel: null,
    comparableWithCapped: false,
  },
};

function opportunity(
  id: string,
  title: string,
  projection: InterventionProjection | null,
): OpportunityCandidate {
  return {
    id,
    title,
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
    projection,
  };
}

const AUDIT: ReviewAuditEvent[] = [];

function blueprint(projection: InterventionProjection | null): BlueprintResponse {
  return {
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
    projection,
  };
}

function stage(opportunities: OpportunityCandidate[]): RoadmapStage {
  return {
    id: 'NEXT_30',
    title: 'Next 30 Days',
    summary: 'Prove value fast with low-effort quick wins.',
    opportunities,
    requiredPermissions: [],
    dependencies: [],
  };
}

// ---------------------------------------------------------------------------
// Opportunity Review — the band and its evidence label.
// ---------------------------------------------------------------------------

describe('2.0-A1 T4 — Opportunity Review shows the band and its evidence label', () => {
  it('renders the resulting band, its qualitative width tier, and the evidence label', () => {
    render(<OpportunityDetail opp={opportunity('opp_001', 'Routing friction', CORROBORATED)} audit={AUDIT} />);

    const panel = screen.getByTestId('projection-band-panel');
    expect(within(panel).getByTestId('projection-band-range')).toHaveTextContent(
      '23-57% of the recurring instances',
    );
    expect(within(panel).getByTestId('projection-band-tier')).toHaveTextContent(
      'Moderate band',
    );
    expect(within(panel).getByTestId('projection-band-tier')).not.toHaveTextContent('34 pts');
    expect(within(panel).getByTestId('projection-evidence-label')).toHaveTextContent(
      'Strong evidence',
    );
  });

  it('keeps the band explanation high level and leaves evidence inputs to Projection Basis', () => {
    render(<OpportunityDetail opp={opportunity('opp_001', 'Routing friction', CORROBORATED)} audit={AUDIT} />);

    const panel = screen.getByTestId('projection-band-panel');
    expect(within(panel).getByTestId('projection-band-rationale')).toHaveTextContent(
      'See Projection Basis',
    );
    expect(panel).not.toHaveTextContent('800 observed');
    expect(panel).not.toHaveTextContent('strong sample');
    expect(panel).not.toHaveTextContent('steady recurrence');
    expect(panel).not.toHaveTextContent('corroborated corroboration');
    // Evidence inputs are shown once in Projection Basis, not repeated here.
    for (const axis of [
      'sample_size',
      'recurrence_stability',
      'corroboration_status',
      'confidence_cap',
    ]) {
      expect(within(panel).queryByTestId(`band-width-driver-${axis}`)).not.toBeInTheDocument();
    }
    expect(panel).not.toHaveTextContent('+4.5 pts');
    expect(panel).not.toHaveTextContent('no widening');
    expect(panel).not.toHaveTextContent('band model');
    expect(within(panel).queryByTestId('projection-strength-value')).not.toBeInTheDocument();
  });

  it('labels a capped single-source projection and shows its wider band', () => {
    render(<OpportunityDetail opp={opportunity('opp_001', 'Routing friction', CAPPED)} audit={AUDIT} />);

    const panel = screen.getByTestId('projection-band-panel');
    expect(within(panel).getByTestId('projection-band-range')).toHaveTextContent(
      '13-67%',
    );
    expect(within(panel).getByTestId('projection-band-tier')).toHaveTextContent(
      'Wide band',
    );
    expect(within(panel).getByTestId('projection-evidence-label')).toHaveTextContent(
      'Adequate evidence - band widened',
    );
    expect(within(panel).getByTestId('projection-capped-label')).toHaveTextContent(
      'Capped - single-source confidence',
    );
    expect(within(panel).getByTestId('projection-capped-label')).toHaveTextContent(
      'treat the range as directional until corroborated',
    );
  });

  it('says so plainly when there is no band, rather than rendering nothing', () => {
    render(<OpportunityDetail opp={opportunity('opp_001', 'Routing friction', NO_BAND)} audit={AUDIT} />);

    expect(screen.getByTestId('projection-band-panel')).toHaveTextContent(
      'No material change projected',
    );
    expect(screen.queryByTestId('projection-band-range')).not.toBeInTheDocument();
  });

  it('renders nothing at all for an opportunity with no projection', () => {
    render(<OpportunityDetail opp={opportunity('opp_001', 'Routing friction', null)} audit={AUDIT} />);

    expect(screen.queryByTestId('projection-band-panel')).not.toBeInTheDocument();
  });

  it('never shows a point estimate or savings language', () => {
    render(<OpportunityDetail opp={opportunity('opp_001', 'Routing friction', CORROBORATED)} audit={AUDIT} />);

    const text = screen.getByTestId('projection-band-panel').textContent ?? '';
    for (const phrase of [
      'will save',
      'will reduce',
      'will cut',
      'guarantee',
      'savings',
      'ROI',
    ]) {
      expect(text.toLowerCase()).not.toContain(phrase.toLowerCase());
    }
    // A band, never a single number.
    expect(text).toMatch(/\d+-\d+%/);
  });
});

// ---------------------------------------------------------------------------
// Agentforce Blueprint — the customer-facing band travels with the agent design.
// ---------------------------------------------------------------------------

describe('2.0-A1 T4 — Agentforce Blueprint shows the customer-facing band', () => {
  it('renders the band and evidence label without duplicate strength details', () => {
    render(<BlueprintContent blueprint={blueprint(CORROBORATED)} />);

    const compact = screen.getByTestId('projection-band-compact');
    expect(screen.getByText('Projection Band')).toBeInTheDocument();
    expect(within(compact).getByTestId('projection-band-range')).toHaveTextContent(
      '23-57%',
    );
    expect(within(compact).getByTestId('projection-band-tier')).toHaveTextContent(
      'Moderate band',
    );
    expect(within(compact).getByTestId('projection-evidence-label')).toHaveTextContent(
      'Strong evidence',
    );
    expect(within(compact).queryByTestId('projection-strength')).not.toBeInTheDocument();
  });

  it('leaves capped-confidence details to Opportunity Review', () => {
    render(<BlueprintContent blueprint={blueprint(CAPPED)} />);

    const compact = screen.getByTestId('projection-band-compact');
    expect(within(compact).queryByTestId('projection-strength')).not.toBeInTheDocument();
    expect(within(compact).queryByTestId('projection-capped-label')).not.toBeInTheDocument();
  });

  it('omits the band section entirely when the finding carries no band', () => {
    render(<BlueprintContent blueprint={blueprint(NO_BAND)} />);

    expect(screen.queryByTestId('projection-band-compact')).not.toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Agent Roadmap — projection strength used carefully in ordering and display.
// ---------------------------------------------------------------------------

describe('2.0-A1 T4 / AC4 — Agent Roadmap ordering', () => {
  const capped = opportunity('opp_capped', 'Capped finding', CAPPED);
  const corroborated = opportunity('opp_corroborated', 'Corroborated finding', CORROBORATED);

  it('never presents a capped finding above a corroborated equivalent', () => {
    // Supplied capped-first, so only the AC4 rule can produce the expected order.
    render(
      <StageCard stage={stage([capped, corroborated])} onOpenReview={vi.fn()} />,
    );

    const rows = screen.getAllByTestId(/^opp-row-/);
    expect(rows.map((row) => row.getAttribute('data-testid'))).toEqual([
      'opp-row-opp_corroborated',
      'opp-row-opp_capped',
    ]);
    expect(within(screen.getByTestId('opp-row-opp_capped')).queryByText(
      /Capped.*single-source confidence/i,
    )).not.toBeInTheDocument();
  });

  it('preserves every other ordering decision — it demotes, it does not re-rank', () => {
    const weakUncapped = opportunity('opp_weak', 'Weak but corroborated', {
      ...CORROBORATED,
      projectionStrength: {
        ...CORROBORATED.projectionStrength!,
        value: 0.3,
        tier: 'weak',
        label: 'Weak projection strength',
      },
    });

    render(
      <StageCard
        stage={stage([weakUncapped, capped, corroborated])}
        onOpenReview={vi.fn()}
      />,
    );

    const rows = screen.getAllByTestId(/^opp-row-/);
    expect(rows.map((row) => row.getAttribute('data-testid'))).toEqual([
      'opp-row-opp_weak',
      'opp-row-opp_corroborated',
      'opp-row-opp_capped',
    ]);
  });

  it('shows the band without duplicate strength details on each roadmap row', () => {
    render(<StageCard stage={stage([corroborated])} onOpenReview={vi.fn()} />);

    const row = screen.getByTestId('opp-row-opp_corroborated');
    expect(within(row).getByTestId('opp-band-opp_corroborated')).toHaveTextContent(
      'Projected 23-57% of the recurring instances',
    );
    expect(within(row).queryByTestId('projection-strength')).not.toBeInTheDocument();
  });

  it('treats an opportunity with no projection as uncapped and leaves it in place', () => {
    const none = opportunity('opp_none', 'No projection', null);
    render(<StageCard stage={stage([capped, none])} onOpenReview={vi.fn()} />);

    const rows = screen.getAllByTestId(/^opp-row-/);
    expect(rows.map((row) => row.getAttribute('data-testid'))).toEqual([
      'opp-row-opp_none',
      'opp-row-opp_capped',
    ]);
  });
});

// ---------------------------------------------------------------------------
// The ordering helpers themselves.
// ---------------------------------------------------------------------------

describe('2.0-A1 T4 — projection strength ordering helpers', () => {
  it('sorts every capped projection below every uncapped one', () => {
    expect(projectionRankKey(CORROBORATED)[0]).toBe(0);
    expect(projectionRankKey(CAPPED)[0]).toBe(1);
  });

  it('does not let a large capped strength overtake a small uncapped one', () => {
    const weakUncapped: InterventionProjection = {
      ...CORROBORATED,
      projectionStrength: { ...CORROBORATED.projectionStrength!, value: 0.05 },
    };
    const ordered = orderByProjectionStrength([CAPPED, weakUncapped], (p) => p);
    expect(ordered[0]).toBe(weakUncapped);
  });

  it('sorts a bandless projection last within its group', () => {
    const ordered = orderByProjectionStrength([NO_BAND, CORROBORATED], (p) => p);
    expect(ordered[0]).toBe(CORROBORATED);
  });

  it('recognises a pre-T4 stored projection as capped from the top-level flag', () => {
    const legacy = {
      ...CORROBORATED,
      projectionStrength: undefined,
      bandWidth: undefined,
      confidenceCapped: true,
    } as InterventionProjection;
    const ordered = demoteCappedProjections([legacy, CORROBORATED], (p) => p);
    expect(ordered[0]).toBe(CORROBORATED);
  });

  it('is stable for equally-ranked items', () => {
    const items = [CORROBORATED, { ...CORROBORATED }];
    expect(orderByProjectionStrength(items, (p) => p)).toEqual(items);
  });
});
