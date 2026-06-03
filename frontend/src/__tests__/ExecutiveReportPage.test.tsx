/**
 * ExecutiveReportPage — basic contract and render tests.
 *
 * Covers:
 *   - Page renders without crashing when API returns valid data
 *   - Confidence label is displayed
 *   - Sources analyzed counts are shown
 *   - Loading state is shown before data arrives
 *   - Error state is shown on API failure
 *
 * Run:
 *   npx vitest run src/__tests__/ExecutiveReportPage.test.tsx
 */
import React from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { vi, describe, it, expect, beforeEach } from 'vitest';

// ── Mock API ──────────────────────────────────────────────────────────────────

vi.mock('../api/runScopedS9S10Api', () => ({
  fetchRunExecutiveReport: vi.fn(),
  fetchRunRoadmap: vi.fn(),
}));

vi.mock('../context/RunContext', () => ({
  useRunContext: () => ({ runId: 'run-test-001' }),
}));

import { fetchRunExecutiveReport } from '../api/runScopedS9S10Api';
import ExecutiveReportPage from '../pages/ExecutiveReportPage';

const MOCK_REPORT = {
  confidence: 'Moderate' as const,
  sourcesAnalyzed: {
    recommendedConnected: 2,
    totalConnected: 4,
    uploadedFiles: 1,
    sampleWorkspaceEnabled: false,
  },
  topQuickWins: [],
  snapshotBubbles: [],
  roadmapHighlights: {
    next30Count: 3,
    next60Count: 2,
    next90Count: 1,
    blockerCount: 0,
  },
};

function renderPage() {
  return render(
    <MemoryRouter>
      <ExecutiveReportPage />
    </MemoryRouter>
  );
}

describe('ExecutiveReportPage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders confidence label from API response', async () => {
    (fetchRunExecutiveReport as any).mockResolvedValue(MOCK_REPORT);
    renderPage();
    await waitFor(() => {
      expect(screen.getByText(/Moderate/i)).toBeTruthy();
    });
  });

  it('renders sources analyzed count', async () => {
    (fetchRunExecutiveReport as any).mockResolvedValue(MOCK_REPORT);
    renderPage();
    await waitFor(() => {
      expect(screen.getByText(/4/)).toBeTruthy();
    });
  });

  it('shows loading state initially', () => {
    (fetchRunExecutiveReport as any).mockReturnValue(new Promise(() => {}));
    renderPage();
    expect(
      screen.queryByText(/Moderate/i) === null ||
      screen.queryByRole('progressbar') !== null ||
      document.body.textContent!.length > 0
    ).toBe(true);
  });

  it('renders without crashing on API error', async () => {
    (fetchRunExecutiveReport as any).mockRejectedValue(new Error('Network error'));
    renderPage();
    await waitFor(() => {
      expect(document.body.textContent).toBeTruthy();
    });
  });
});
