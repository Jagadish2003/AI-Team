/**
 * ExecutiveReportPage — "download" feature tests
 *
 * Covers the Executive Report download controls:
 *   - "Download PDF" is the only enabled export and, when clicked, invokes the
 *     PDF export util with the report data + a dated filename.
 *   - "Download PPTX" and "Download XLSX" are disabled.
 *   - The export data mirrors the report (confidence, quick wins, the LLM
 *     summary, org name, run id).
 *
 * Run:
 *   npx vitest run src/__tests__/ExecutiveReportPage.test.tsx
 */
import React from 'react';
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { vi, describe, it, expect, beforeEach } from 'vitest';
import type { OpportunityCandidate } from '../types/analystReview';
import type { ExecutiveReport } from '../api/runScopedS9S10Api';
import type { RunEnrichment } from '../api/enrichmentApi';

// Fixtures + mock fns live in vi.hoisted so the (hoisted) vi.mock factories
// below can reference them.
const h = vi.hoisted(() => {
  const QUICK_WIN: OpportunityCandidate = {
    id: 'opp_qw',
    title: 'Checklist Bottleneck',
    category: 'Automation Opportunity',
    tier: 'Quick Win',
    impact: 7,
    effort: 2,
    confidence: 'HIGH',
    aiRationale: 'High manual checklist effort detected.',
    evidenceIds: ['ev_1'],
    decision: 'UNREVIEWED',
    override: { isLocked: false, rationaleOverride: '', overrideReason: '', updatedAt: null },
    requiredPermissions: [],
  };
  const STRATEGIC: OpportunityCandidate = {
    ...QUICK_WIN,
    id: 'opp_str',
    title: 'Covenant Tracking Gap',
    tier: 'Strategic',
    impact: 8,
    effort: 6,
  };
  const REPORT: ExecutiveReport = {
    confidence: 'High',
    sourcesAnalyzed: { recommendedConnected: 3, totalConnected: 3, uploadedFiles: 0 },
    topQuickWins: [],
    snapshotBubbles: [],
    roadmapHighlights: {},
  };
  const ENRICHMENT: RunEnrichment = {
    runId: 'run_x',
    executiveSummary: 'LLM generated executive summary for the board.',
    opportunitiesEnriched: 2,
    opportunitiesFailed: 0,
    generatedAt: null,
    llmModel: 'claude',
    available: true,
  };
  return {
    QUICK_WIN,
    STRATEGIC,
    REPORT,
    ENRICHMENT,
    mockDownloadPdf: vi.fn().mockResolvedValue(undefined),
    mockPush: vi.fn(),
  };
});

vi.mock('../utils/exportPdf', () => ({
  downloadExecutiveReportPdf: (...args: unknown[]) => h.mockDownloadPdf(...args),
}));

vi.mock('../api/runScopedS9S10Api', () => ({
  fetchRunExecutiveReport: vi.fn().mockResolvedValue(h.REPORT),
}));

vi.mock('../api/enrichmentApi', () => ({
  fetchRunEnrichment: vi.fn().mockResolvedValue(h.ENRICHMENT),
}));

vi.mock('../components/common/Toast', () => ({
  useToast: () => ({ push: h.mockPush }),
}));

vi.mock('../components/common/TopNav', () => ({
  default: () => <nav data-testid="top-nav" />,
}));

vi.mock('../context/RunContext', () => ({
  useRunContext: () => ({ runId: 'run_x' }),
}));

vi.mock('../context/DiscoveryRunContext', () => ({
  useDiscoveryRunContext: () => ({ run: { status: 'complete' }, computing: false }),
}));

vi.mock('../context/AnalystReviewContext', () => ({
  useAnalystReviewContext: () => ({ opportunities: [h.QUICK_WIN, h.STRATEGIC], select: vi.fn() }),
}));

vi.mock('../context/AuthContext', () => ({
  useAuthOptional: () => ({ user: { org_name: 'Acme Bank' } }),
}));

import ExecutiveReportPage from '../pages/ExecutiveReportPage';

function renderPage() {
  return render(
    <MemoryRouter initialEntries={['/executive-report?runId=run_x']}>
      <ExecutiveReportPage />
    </MemoryRouter>,
  );
}

async function findDownloadPdfButton() {
  return waitFor(() => screen.getByRole('button', { name: /download pdf/i }));
}

describe('ExecutiveReportPage — download controls', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders an enabled "Download PDF" button once the report loads', async () => {
    renderPage();
    const pdfBtn = await findDownloadPdfButton();
    expect(pdfBtn).toBeEnabled();
  });

  it('disables the PPTX and XLSX export buttons', async () => {
    renderPage();
    await findDownloadPdfButton();
    expect(screen.getByRole('button', { name: /download pptx/i })).toBeDisabled();
    expect(screen.getByRole('button', { name: /download xlsx/i })).toBeDisabled();
  });

  it('invokes the PDF export with report data and a dated filename when clicked', async () => {
    renderPage();
    const pdfBtn = await findDownloadPdfButton();

    await act(async () => {
      fireEvent.click(pdfBtn);
    });

    await waitFor(() => expect(h.mockDownloadPdf).toHaveBeenCalledTimes(1));
    const [data, options] = h.mockDownloadPdf.mock.calls[0];

    // Options: dated filename + confidential footer.
    expect(options.filename).toMatch(/^AgentIQ-Executive-Report-\d{4}-\d{2}-\d{2}\.pdf$/);
    expect(options.footerText).toMatch(/confidential/i);

    // Data mirrors the report.
    expect(data.confidence).toBe('High');
    expect(data.sourcesLabel).toBe('3 Connected');
    expect(data.summary).toMatch(/LLM generated executive summary for the board/i);
    expect(data.orgName).toBe('Acme Bank');
    expect(data.runId).toBe('run_x');
    expect(data.opportunities).toHaveLength(2);
    expect(data.quickWins.map((o: OpportunityCandidate) => o.title)).toContain('Checklist Bottleneck');
  });
});
