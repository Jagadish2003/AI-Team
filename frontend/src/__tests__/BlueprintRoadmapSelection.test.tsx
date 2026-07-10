/**
 * Regression — Agent Roadmap row click drives the Blueprint selection.
 *
 * The Agent Roadmap and Agentforce Blueprint are merged on one page. Clicking
 * an opportunity row in the roadmap selects it in-page WITHOUT changing the URL.
 * A previous bug re-asserted the URL's stale ?oppId on every selection change,
 * so a row click was immediately reverted to the URL's opportunity and the
 * Blueprint never reflected the roadmap click. This test locks the fix.
 */
import React from 'react';
import { cleanup, render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { OpportunityCandidate } from '../types/analystReview';
import type { PilotRoadmapModel } from '../types/pilotRoadmap';

function mkOpp(id: string, title: string, tier: OpportunityCandidate['tier']): OpportunityCandidate {
  return {
    id,
    title,
    category: 'Automation',
    tier,
    impact: 3,
    effort: 2,
    confidence: 'MEDIUM',
    aiRationale: '',
    evidenceIds: [],
    decision: 'UNREVIEWED',
    override: { isLocked: false, rationaleOverride: '', overrideReason: '', updatedAt: null },
  };
}

const OPP_A = mkOpp('opp_a', 'Automate repetitive Case processing flows', 'Quick Win');
const OPP_B = mkOpp('opp_b', "Redistribute approval workload for 'Discount Approval'", 'Quick Win');

const state = vi.hoisted(() => ({
  selectedId: null as string | null,
}));

const select = vi.fn((id: string | null) => {
  state.selectedId = id;
});

vi.mock('../context/RunContext', () => ({
  useRunContext: () => ({ runId: 'run_sel' }),
}));

vi.mock('../context/ConnectorContext', () => ({
  useConnectorContext: () => ({ all: [{ id: 'salesforce', status: 'connected' }] }),
}));

vi.mock('../context/DiscoveryRunContext', () => ({
  useDiscoveryRunContext: () => ({ run: { status: 'complete' }, computing: false }),
}));

vi.mock('../context/AnalystReviewContext', () => ({
  useAnalystReviewContext: () => ({
    opportunities: [OPP_A, OPP_B],
    selectedId: state.selectedId,
    select,
  }),
}));

const roadmapModel: PilotRoadmapModel = {
  selectedOpportunityCount: 2,
  requiredPermissionsCount: 0,
  dependencyCount: 0,
  overallReadiness: 'Low',
  stages: [
    {
      id: 'NEXT_30',
      title: 'Next 30 Days',
      summary: '',
      opportunities: [OPP_A, OPP_B],
      requiredPermissions: [],
      dependencies: [],
    },
    { id: 'NEXT_60', title: 'Next 60 Days', summary: '', opportunities: [], requiredPermissions: [], dependencies: [] },
    { id: 'NEXT_90', title: 'Next 90 Days', summary: '', opportunities: [], requiredPermissions: [], dependencies: [] },
  ],
};

vi.mock('../api/runScopedS9S10Api', () => ({
  fetchRunRoadmap: vi.fn(() => Promise.resolve(roadmapModel)),
}));

vi.mock('../api/blueprintApi', () => ({
  fetchBlueprint: vi.fn((_runId: string, oppId: string) =>
    Promise.resolve({
      opportunityId: oppId,
      agentName: `Agent for ${oppId}`,
      detectorId: 'DET',
      agentTopic: '',
      suggestedActions: [],
      guardrails: [],
      agentforcePermissions: [],
      evidenceIds: [],
      complexity: { label: '', description: '', tier: '' },
    }),
  ),
}));

vi.mock('../api/runApi', () => ({
  fetchEvidence: vi.fn(() => Promise.resolve([])),
}));

vi.mock('../components/common/PageShell', () => ({
  default: ({ children }: any) => <main>{children}</main>,
}));

import BlueprintPage from '../pages/BlueprintPage';

beforeEach(() => {
  state.selectedId = null;
  select.mockClear();
  // jsdom does not implement scrollIntoView; the page calls it on selection.
  Element.prototype.scrollIntoView = vi.fn();
});

afterEach(() => {
  cleanup();
});

describe('Agent Roadmap → Blueprint selection', () => {
  it('selects the clicked roadmap opportunity even when the URL carries a stale oppId', async () => {
    // Arrive on the page with a stale ?oppId=opp_a (e.g. from an Opportunity
    // Review bubble). The URL effect applies it once.
    render(
      <MemoryRouter initialEntries={['/agentforce-blueprint?runId=run_sel&oppId=opp_a']}>
        <BlueprintPage />
      </MemoryRouter>,
    );

    await waitFor(() => expect(select).toHaveBeenCalledWith('opp_a'));
    expect(state.selectedId).toBe('opp_a');

    // Now click a DIFFERENT opportunity row in the Agent Roadmap. The row click
    // selects without changing the URL, so the stale ?oppId=opp_a must not
    // revert it.
    const row = await screen.findByTestId('opp-row-opp_b');
    fireEvent.click(row);

    await waitFor(() => expect(state.selectedId).toBe('opp_b'));

    // The stale URL param must never pull the selection back to opp_a.
    await new Promise((r) => setTimeout(r, 50));
    expect(state.selectedId).toBe('opp_b');
  });
});
