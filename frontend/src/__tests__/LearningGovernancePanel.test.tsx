import React from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { DataCacheProvider, useResource } from '../lib/dataCache';
import type {
  LearningAdjustmentHistoryEntry,
  LearningAdjustmentResetResponse,
  LearningAdjustmentStateResponse,
} from '../types/learning';

const h = vi.hoisted(() => ({
  role: { current: 'owner' as 'owner' | 'analyst' | 'viewer' },
  fetchState: vi.fn(),
  fetchHistory: vi.fn(),
  reset: vi.fn(),
  fetchRun: vi.fn(),
}));

vi.mock('../context/AuthContext', () => ({
  useAuthOptional: () => ({ user: { role: h.role.current } }),
}));

vi.mock('../api/learningApi', () => ({
  fetchLearningAdjustmentState: (...args: unknown[]) => h.fetchState(...args),
  fetchLearningAdjustmentHistory: (...args: unknown[]) => h.fetchHistory(...args),
  resetLearningAdjustment: (...args: unknown[]) => h.reset(...args),
}));

import LearningGovernancePanel from '../components/settings/LearningGovernancePanel';

const ACTIVATION = {
  status: 'active' as const,
  isActive: true,
  message: null,
  currentCount: 12,
  threshold: 5,
  counts: {
    weightedSignals: 12,
    decisions: 8,
    outcomes: 2,
    distinctIdentities: 5,
  },
  thresholds: {
    minimumDecisions: 5,
    minimumSignals: 8,
    minimumDistinctIdentities: 3,
  },
  remaining: {
    decisions: 0,
    weightedSignals: 0,
    distinctIdentities: 0,
  },
};

const ACTIVE_STATE: LearningAdjustmentStateResponse = {
  orgId: 'org-1',
  enabled: true,
  caps: {
    maxScoreFraction: 0.15,
    maxRankMove: 2,
    pointsPerSignalUnit: 0.02,
  },
  configVersion: 'learning-1',
  learningState: ACTIVATION,
  groups: [
    {
      detectorId: 'handoff_friction',
      packId: 'service_cloud',
      signalConcept: 'handoff_friction',
      netWeight: 0.35,
      outcomeWeight: 0.25,
      decisionWeight: 0.1,
      hasOutcomeEvidence: true,
      signalCount: 6,
      learningActive: true,
      contributingRefs: [],
      configVersion: 'learning-1',
      revision: 3,
      computedAt: '2026-08-08T09:30:00Z',
      updatedAt: '2026-08-08T09:30:00Z',
    },
  ],
};

const HISTORY: LearningAdjustmentHistoryEntry[] = [
  {
    schemaVersion: '1.0.0',
    historyId: 'history-reset',
    orgId: 'org-1',
    detectorId: 'handoff_friction',
    packId: 'service_cloud',
    changeKind: 'reset',
    previousNetWeight: 0.2,
    netWeight: 0,
    signalCount: 0,
    learningActive: false,
    actorId: 'owner@example.com',
    configVersion: 'learning-1',
    revision: 4,
    recordedAt: '2026-08-09T10:00:00Z',
    resetReason: 'Quarterly governance review',
  },
  {
    schemaVersion: '1.0.0',
    historyId: 'history-active',
    orgId: 'org-1',
    detectorId: 'handoff_friction',
    packId: 'service_cloud',
    changeKind: 'activated',
    previousNetWeight: null,
    netWeight: 0.2,
    signalCount: 5,
    learningActive: true,
    actorId: 'system',
    configVersion: 'learning-1',
    revision: 1,
    recordedAt: '2026-08-01T10:00:00Z',
  },
];

const NEUTRAL_STATE: LearningAdjustmentStateResponse = {
  ...ACTIVE_STATE,
  learningState: {
    ...ACTIVATION,
    status: 'learning_not_yet_active',
    isActive: false,
    message: 'Learning was reset. Rankings use base order.',
  },
  groups: ACTIVE_STATE.groups.map((group) => ({
    ...group,
    netWeight: 0,
    outcomeWeight: 0,
    decisionWeight: 0,
    hasOutcomeEvidence: false,
    signalCount: 0,
    learningActive: false,
    revision: 4,
    updatedAt: '2026-08-09T10:00:00Z',
  })),
};

const RESET_RESPONSE: LearningAdjustmentResetResponse = {
  schemaVersion: '1.0.0',
  orgId: 'org-1',
  changeKind: 'reset',
  groupsReset: 1,
  opportunitiesAffected: 3,
  previousState: ACTIVE_STATE.groups,
  currentState: NEUTRAL_STATE.groups,
  configVersion: 'learning-1',
  resetAt: '2026-08-09T10:00:00Z',
  actorId: 'owner@example.com',
  reason: 'Return to baseline for quarterly review',
};

function RunRankingProbe() {
  const resource = useResource('runs/run-1/opportunities', () => h.fetchRun());
  return <span data-testid="run-probe">{resource.data ? 'loaded' : 'loading'}</span>;
}

function renderPanel(withRunProbe = false) {
  return render(
    <DataCacheProvider>
      <LearningGovernancePanel />
      {withRunProbe && <RunRankingProbe />}
    </DataCacheProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  h.role.current = 'owner';
  h.fetchState.mockResolvedValue(ACTIVE_STATE);
  h.fetchHistory.mockResolvedValue(HISTORY);
  h.fetchRun.mockResolvedValue([{ id: 'opportunity-1' }]);
  h.reset.mockImplementation(async () => {
    h.fetchState.mockResolvedValue(NEUTRAL_STATE);
    h.fetchHistory.mockResolvedValue([
      {
        ...HISTORY[0],
        historyId: 'history-new-reset',
        resetReason: 'Return to baseline for quarterly review',
      },
      ...HISTORY,
    ]);
    return RESET_RESPONSE;
  });
});

describe('LearningGovernancePanel', () => {
  it('shows understandable current state, caps, and complete history to an Owner', async () => {
    renderPanel();

    expect(await screen.findByText('Bounded learning is active')).toBeInTheDocument();
    expect(screen.getByText('At most 2 positions')).toBeInTheDocument();
    expect(screen.getByText('Up to 15% of base impact')).toBeInTheDocument();
    expect(screen.getByText('Handoff Friction')).toBeInTheDocument();
    expect(screen.getByText('May rank similar opportunities higher')).toBeInTheDocument();
    expect(screen.getByText('Includes measured outcomes')).toBeInTheDocument();

    const history = screen.getByTestId('learning-adjustment-history');
    expect(history).toHaveTextContent('Owner reset to neutral');
    expect(history).toHaveTextContent('Learning activated');
    expect(history).toHaveTextContent('Quarterly governance review');
    expect(history).toHaveTextContent('owner@example.com');
    expect(h.fetchHistory).toHaveBeenCalledWith(1000);

    // Internal weights and tuning increments are deliberately not customer-facing.
    expect(screen.queryByText('0.35')).not.toBeInTheDocument();
    expect(screen.queryByText('0.02')).not.toBeInTheDocument();
  });

  it('does not render or call Owner endpoints for Analysts', async () => {
    h.role.current = 'analyst';
    renderPanel();

    expect(screen.queryByTestId('learning-governance-panel')).not.toBeInTheDocument();
    await waitFor(() => {
      expect(h.fetchState).not.toHaveBeenCalled();
      expect(h.fetchHistory).not.toHaveBeenCalled();
    });
  });

  it('requires an audited reason, resets to neutral, and refreshes served rankings', async () => {
    const user = userEvent.setup();
    renderPanel(true);

    expect(await screen.findByText('Bounded learning is active')).toBeInTheDocument();
    await waitFor(() => expect(h.fetchRun).toHaveBeenCalledTimes(1));

    await user.click(screen.getByRole('button', { name: 'Reset learning' }));
    expect(screen.getByRole('dialog', { name: 'Reset ranking learning?' })).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Reset to neutral' }));
    expect(await screen.findByText('Enter a reason for the audit history.')).toBeInTheDocument();
    expect(h.reset).not.toHaveBeenCalled();

    await user.type(
      screen.getByLabelText(/Reason/),
      '  Return to baseline for quarterly review  ',
    );
    await user.click(screen.getByRole('button', { name: 'Reset to neutral' }));

    await waitFor(() => {
      expect(h.reset).toHaveBeenCalledWith('Return to baseline for quarterly review');
    });
    expect(await screen.findByText('Base order is active')).toBeInTheDocument();
    expect(screen.getByRole('status')).toHaveTextContent(
      'Base order restored for 3 opportunities.',
    );
    expect(await screen.findByText(/Return to baseline for quarterly review/)).toBeInTheDocument();

    await waitFor(() => {
      expect(h.fetchState.mock.calls.length).toBeGreaterThanOrEqual(2);
      expect(h.fetchHistory.mock.calls.length).toBeGreaterThanOrEqual(2);
      expect(h.fetchRun.mock.calls.length).toBeGreaterThanOrEqual(2);
    });
  });

  it('keeps the confirmation open and explains a failed reset', async () => {
    const user = userEvent.setup();
    h.reset.mockRejectedValueOnce(new Error('network unavailable'));
    renderPanel();

    await screen.findByText('Bounded learning is active');
    await user.click(screen.getByRole('button', { name: 'Reset learning' }));
    await user.type(screen.getByLabelText(/Reason/), 'Owner requested review');
    await user.click(screen.getByRole('button', { name: 'Reset to neutral' }));

    expect(await screen.findByText('Could not reset ranking learning. Please try again.')).toBeInTheDocument();
    expect(screen.getByRole('dialog', { name: 'Reset ranking learning?' })).toBeInTheDocument();
  });
});
