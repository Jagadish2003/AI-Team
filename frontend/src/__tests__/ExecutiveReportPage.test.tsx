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
import { DataCacheProvider } from '../lib/dataCache';
import type { OpportunityCandidate } from '../types/analystReview';
import type { ExecutiveReport } from '../api/runScopedS9S10Api';
import type { RunEnrichment } from '../api/enrichmentApi';
import { LEADERSHIP_ACTIONS } from '../components/executive_report/KeyInsights';

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

vi.mock('../context/ConnectorContext', () => ({
  useConnectorContext: () => ({ all: [] }),
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

// R17-D4 Addendum A §2 / T13 — the report carries the license-resolved org name
// (useOrgName, from T12's org-name endpoint), NOT the ad-hoc auth org_name, so the
// PDF identifies the correct organisation.
vi.mock('../context/LicenseContext', () => ({
  useOrgName: () => 'Teachers Credit Union',
}));

import ExecutiveReportPage from '../pages/ExecutiveReportPage';

function renderPage() {
  return render(
    <MemoryRouter initialEntries={['/executive-report?runId=run_x']}>
      <DataCacheProvider>
        <ExecutiveReportPage />
      </DataCacheProvider>
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

  it('uses flexible quick-win approval wording on screen and in the shared PDF actions', async () => {
    renderPage();
    await findDownloadPdfButton();

    const expected = 'Approve top quick wins and confirm success metrics.';
    expect(screen.getByText(expected)).toBeInTheDocument();
    expect(LEADERSHIP_ACTIONS[0]).toBe(expected);
    expect(expected).toContain('Approve top quick wins');
    expect(expected).not.toMatch(/Approve top \d+ quick wins/i);
    expect(document.body).not.toHaveTextContent('Approve top 2 quick wins');
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
    // Org name comes from the license-resolved name (useOrgName), not auth.
    expect(data.orgName).toBe('Teachers Credit Union');
    expect(data.runId).toBe('run_x');
    expect(data.opportunities).toHaveLength(2);
    expect(data.quickWins.map((o: OpportunityCandidate) => o.title)).toContain('Checklist Bottleneck');
  });

  it('shows an error toast and re-enables the button when the export fails', async () => {
    h.mockDownloadPdf.mockRejectedValueOnce(new Error('boom'));
    renderPage();
    const pdfBtn = await findDownloadPdfButton();

    await act(async () => {
      fireEvent.click(pdfBtn);
    });

    // Error toast surfaced.
    await waitFor(() =>
      expect(h.mockPush).toHaveBeenCalledWith(expect.stringMatching(/could not generate/i), 'error'),
    );
    // Button is not stuck in the busy ("Generating PDF…") state.
    await waitFor(() => {
      const btn = screen.getByRole('button', { name: /download pdf/i });
      expect(btn).toBeEnabled();
    });
  });
});
