/**
 * exportPdf — runtime verification of the native (selectable-text) PDF builder.
 *
 * Unlike the page test (which mocks the exporter), this exercises the REAL
 * downloadExecutiveReportPdf against the real jsPDF. It captures the generated
 * jsPDF instance (and neutralizes the actual download), then inspects the
 * uncompressed PDF bytes for the literal text strings. Finding them proves the
 * report is built from real, selectable text operators — not a flat image.
 *
 * Run:
 *   npx vitest run src/__tests__/exportPdf.test.ts
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import type { ExecutiveReportPdfData } from '../utils/exportPdf';
import type { OpportunityCandidate } from '../types/analystReview';

// Subclass jsPDF so save() captures the instance instead of triggering a
// browser download (which would fail in jsdom).
const cap = vi.hoisted(() => ({ pdf: null as unknown as { output: (t: string) => string; getNumberOfPages: () => number } }));

vi.mock('jspdf', async (importOriginal) => {
  const actual = await importOriginal<typeof import('jspdf')>();
  class TestPDF extends actual.jsPDF {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    constructor(...args: any[]) {
      super(...args);
      cap.pdf = this as unknown as typeof cap.pdf;
      // jsPDF assigns save on the instance; neutralize the real download here.
      (this as unknown as { save: () => unknown }).save = () => this;
    }
  }
  return { ...actual, jsPDF: TestPDF, default: TestPDF };
});

import { downloadExecutiveReportPdf } from '../utils/exportPdf';

const opp = (over: Partial<OpportunityCandidate>): OpportunityCandidate => ({
  id: 'x',
  title: 'Opportunity',
  category: 'Automation Opportunity',
  tier: 'Quick Win',
  impact: 7,
  effort: 3,
  confidence: 'HIGH',
  aiRationale: '',
  evidenceIds: [],
  decision: 'UNREVIEWED',
  override: { isLocked: false, rationaleOverride: '', overrideReason: '', updatedAt: null },
  requiredPermissions: [],
  ...over,
});

const DATA: ExecutiveReportPdfData = {
  confidence: 'High',
  sourcesLabel: '3 Connected',
  quickWinsCount: 2,
  roadmapStageLabel: 'Phase 1 / Phase 2 / Phase 3',
  summary: 'Covenant tracking gaps are the highest-impact lending friction. '.repeat(6),
  quickWins: [
    opp({ id: 'a', title: 'Checklist Bottleneck', impact: 7, effort: 2 }),
    opp({ id: 'b', title: 'Loan Origination Routing Friction', impact: 7, effort: 2 }),
  ],
  stageCounts: [2, 2, 0],
  blockerCount: 1,
  overallReadiness: 'High',
  opportunities: [
    opp({ id: 'a', title: 'Checklist Bottleneck', impact: 7, effort: 2 }),
    opp({ id: 'b', title: 'Covenant Tracking Gap', impact: 8, effort: 6 }),
    opp({ id: 'c', title: 'Spreading Bottleneck', impact: 8, effort: 5 }),
  ],
  orgName: 'XYZ',
  generatedAt: 'June 18, 2026',
  runId: 'run_5a62abcd',
};

function decodePdf(uri: string): string {
  const b64 = uri.substring(uri.indexOf('base64,') + 'base64,'.length);
  return atob(b64);
}

describe('downloadExecutiveReportPdf (real jsPDF)', () => {
  beforeEach(() => {
    cap.pdf = null as unknown as typeof cap.pdf;
  });

  it('produces a saved PDF of selectable text covering every section', async () => {
    await downloadExecutiveReportPdf(DATA, {
      filename: 'AgentIQ-Executive-Report-2026-06-18.pdf',
      footerText: 'AgentIQ Executive Report — Confidential',
    });

    expect(cap.pdf).toBeTruthy();
    const bytes = decodePdf(cap.pdf.output('datauristring'));

    // Real, selectable text strings appear in the (uncompressed) content stream.
    expect(bytes).toContain('Executive Report');
    expect(bytes).toContain('Key Insights');
    expect(bytes).toContain('Top Quick Wins');
    expect(bytes).toContain('Agent Roadmap Highlights');
    expect(bytes).toContain('Effort vs Impact');
    expect(bytes).toContain('Checklist Bottleneck');
    // Header: possessive profile line + date line.
    expect(bytes).toContain("XYZ's Profile");
    expect(bytes).toContain('Date: June 18, 2026');
    // PDF text-show operators confirm this is text, not an embedded image.
    expect(bytes).toMatch(/Tj|TJ/);
    expect(cap.pdf.getNumberOfPages()).toBeGreaterThanOrEqual(1);
  });

  it('falls back to the text wordmark when the logo cannot be loaded', async () => {
    // In jsdom there is no server/canvas, so the logo fetch+raster fails — this
    // exercises the logo fallback path. Generation must still succeed and draw
    // the "AgentIQ" wordmark (drawn as separate "Agent" + "IQ" text runs).
    await downloadExecutiveReportPdf(DATA, {
      filename: 'logo-fallback.pdf',
      footerText: 'AgentIQ Executive Report — Confidential',
    });

    expect(cap.pdf).toBeTruthy();
    const bytes = decodePdf(cap.pdf.output('datauristring'));
    // "(IQ)" is the standalone wordmark run — the footer renders "AgentIQ ..."
    // (no standalone "IQ"), so this is specific to the fallback wordmark.
    expect(bytes).toContain('(IQ)');
  });
});
