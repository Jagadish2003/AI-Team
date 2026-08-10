import React from 'react';
import { render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import ExecutiveOutcomeSection from '../components/outcomes/ExecutiveOutcomeSection';
import OpportunityOutcomePanel from '../components/outcomes/OpportunityOutcomePanel';
import OutcomePortfolioPanel from '../components/outcomes/OutcomePortfolioPanel';
import { DataCacheProvider } from '../lib/dataCache';
import type {
  OpportunityOutcomeView,
  OutcomeMeasurement,
  OutcomePortfolioView,
  OutcomeReportSection,
} from '../types/outcome';

const api = vi.hoisted(() => ({
  opportunity: vi.fn(),
  portfolio: vi.fn(),
}));

vi.mock('../api/outcomeApi', () => ({
  fetchOpportunityOutcome: (identity: string) => api.opportunity(identity),
  fetchOutcomePortfolio: (...args: unknown[]) => api.portfolio(...args),
}));

const measurement: OutcomeMeasurement = {
  opportunityIdentity: 'opp_identity',
  detectorId: 'approval_delay',
  actionDate: '2026-07-01',
  measuredAt: '2026-08-01T00:00:00Z',
  baselineRunId: 'run_baseline',
  currentRunId: 'run_current',
  primaryMovement: {
    signalName: 'approval delay',
    baselineValue: 10,
    currentValue: 8,
    delta: -2,
    direction: 'improves',
  },
  movements: [],
  comparability: { verdict: 'weakly_comparable' },
  projectionValidation: { verdict: 'within_band' },
  confounderSummary: {
    count: 1,
    materialCount: 1,
    advisoryCount: 0,
    types: ['pack_version_change'],
  },
  confounders: [
    {
      type: 'pack_version_change',
      severity: 'material',
      label: 'Pack version changed between the two measurements',
      detail: {
        implication:
          'Part of this movement may reflect updated detection logic rather than an operational change.',
      },
    },
  ],
  numberRefs: [],
};

const opportunityView: OpportunityOutcomeView = {
  schemaVersion: '1.0.0',
  orgId: 'org_test',
  opportunityIdentity: 'opp_identity',
  lifecycle: null,
  measurementCount: 1,
  caveatedMeasurementCount: 1,
  latestMeasurement: measurement,
  measurements: [measurement],
  numberRefs: [],
  emptyState: null,
};

const portfolioView: OutcomePortfolioView = {
  schemaVersion: '1.0.0',
  orgId: 'org_test',
  filters: {},
  aggregates: {
    actionedOpportunityCount: 1,
    measuredOpportunityCount: 1,
    measurementCount: 1,
    caveatedMeasurementCount: 1,
    materialCaveatMeasurementCount: 1,
    byDirection: { improves: 1 },
    byComparability: { weakly_comparable: 1 },
    byProjectionValidation: { within_band: 1 },
    numberRefs: [],
  },
  count: 1,
  items: [
    {
      opportunityIdentity: 'opp_identity',
      state: 'measured',
      actionDate: '2026-07-01',
      measurementCount: 1,
      caveatedMeasurementCount: 1,
      latestMeasurement: measurement,
      measurements: [measurement],
      emptyState: null,
    },
  ],
};

const reportSection: OutcomeReportSection = {
  schemaVersion: '1.0.0',
  runId: 'run_current',
  generatedFrom: 'stored_movement_records',
  summary: 'One stored movement comparison is available.',
  aggregates: portfolioView.aggregates,
  highlights: [measurement],
  numberRefs: [],
};

function expectLabelledCaveat() {
  const details = screen.getByTestId('outcome-caveat-details');
  expect(details).toHaveTextContent('Pack version changed between the two measurements');
  expect(details).toHaveTextContent(
    'Part of this movement may reflect updated detection logic rather than an operational change.',
  );
  expect(details).toHaveTextContent('Material');
}

describe('A2 AC3 labelled outcome caveats', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.opportunity.mockResolvedValue(opportunityView);
    api.portfolio.mockResolvedValue(portfolioView);
  });

  it('renders each caveat label and explanation on the opportunity outcome', async () => {
    render(
      <DataCacheProvider>
        <OpportunityOutcomePanel opportunityIdentity="opp_identity" />
      </DataCacheProvider>,
    );
    await screen.findByTestId('outcome-caveat-details');
    expectLabelledCaveat();
  });

  it('renders each latest-measurement caveat on the portfolio view', async () => {
    render(
      <DataCacheProvider>
        <OutcomePortfolioPanel />
      </DataCacheProvider>,
    );
    await screen.findByTestId('outcome-caveat-details');
    expectLabelledCaveat();
  });

  it('renders each highlighted caveat in the executive report section', () => {
    render(<ExecutiveOutcomeSection section={reportSection} />);
    expectLabelledCaveat();
  });
});
