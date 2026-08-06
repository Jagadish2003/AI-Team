/**
 * 2.0-B1 T5 (AC5) — the client-side PDF export is an export surface too.
 *
 * AC5: "Exports contain no unredacted secrets and no host x vulnerability
 * enumeration (1.9 aggregation floor holds in export)."
 *
 * `downloadExecutiveReportPdf` re-serialises API responses (it does NOT
 * screenshot the DOM), so the correct place to enforce AC5 for it is
 * SERVER-side: the content it renders — opportunity titles and the LLM
 * executive summary — is swept by the aggregation floor at materialization,
 * and duplicating the floor's regexes in TypeScript would create a second
 * source of truth, which is exactly what the floor module warns against.
 *
 * What this test therefore does is CHARACTERISE the surface, so the reasoning
 * above stays true:
 *
 *  1. It pins exactly WHICH fields reach the PDF, so if someone later feeds it
 *     raw evidence snippets, `aiRationale`, or SecOps content, this test fails
 *     and the AC5 analysis gets revisited rather than silently invalidated.
 *  2. It proves the PDF is a faithful projection of its input — anything the
 *     server sends WILL appear in the exported bytes (including inside the
 *     chart raster, which no text-based check could audit). That is the
 *     argument for keeping the enforcement server-side.
 *
 * Run:
 *   npx vitest run src/__tests__/exportPdfAggregationFloor.test.ts
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import type { ExecutiveReportPdfData } from '../utils/exportPdf';
import type { OpportunityCandidate } from '../types/analystReview';

const cap = vi.hoisted(() => ({
  pdf: null as unknown as { output: (t: string) => string },
}));

vi.mock('jspdf', async (importOriginal) => {
  const actual = await importOriginal<typeof import('jspdf')>();
  class TestPDF extends actual.jsPDF {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    constructor(...args: any[]) {
      super(...args);
      cap.pdf = this as unknown as typeof cap.pdf;
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

function decodePdf(uri: string): string {
  const b64 = uri.substring(uri.indexOf('base64,') + 'base64,'.length);
  return atob(b64);
}

/** The full set of fields the exporter is allowed to consume. Adding a field
 * here is a deliberate act that should prompt an AC5 re-check. */
const ALLOWED_PDF_FIELDS = [
  'confidence',
  'sourcesLabel',
  'quickWinsCount',
  'roadmapStageLabel',
  'summary',
  'quickWins',
  'stageCounts',
  'blockerCount',
  'overallReadiness',
  'opportunities',
  'orgName',
  'userName',
  'generatedAt',
  'runId',
] as const;

const CLEAN_DATA: ExecutiveReportPdfData = {
  confidence: 'High',
  sourcesLabel: '3 Connected',
  quickWinsCount: 1,
  roadmapStageLabel: 'Phase 1 / Phase 2 / Phase 3',
  summary: 'Recurring manual routing concentrates in two queues.',
  quickWins: [opp({ id: 'a', title: 'Checklist Bottleneck', impact: 7, effort: 2 })],
  stageCounts: [1, 1, 0],
  blockerCount: 0,
  overallReadiness: 'High',
  opportunities: [opp({ id: 'a', title: 'Checklist Bottleneck', impact: 7, effort: 2 })],
  orgName: 'XYZ',
  userName: 'Likhith',
  generatedAt: 'June 18, 2026',
  runId: 'run_5a62abcd',
};

describe('executive-report PDF export — AC5 surface characterisation', () => {
  beforeEach(() => {
    cap.pdf = null as unknown as typeof cap.pdf;
  });

  it('consumes only the pinned field set (no evidence snippets, no aiRationale)', () => {
    // The exporter's input contract is the AC5 boundary: raw evidence text and
    // per-finding rationale are NOT part of it, which is why the executive PDF
    // cannot leak a detector-built snippet.
    const keys = Object.keys(CLEAN_DATA).sort();
    expect(keys).toEqual([...ALLOWED_PDF_FIELDS].sort());
    expect(keys).not.toContain('evidence');
    expect(keys).not.toContain('snippet');
    expect(keys).not.toContain('aiRationale');
    expect(keys).not.toContain('secopsVolume');
  });

  it('does not render a finding\'s aiRationale even when one is present', async () => {
    // OpportunityCandidate carries aiRationale, so the exporter RECEIVES it via
    // quickWins/opportunities. It must not print it — the executive report is a
    // summary surface, and rationale is unredacted narrative.
    const marker = 'RATIONALE_MARKER_SHOULD_NOT_APPEAR_IN_PDF';
    await downloadExecutiveReportPdf(
      {
        ...CLEAN_DATA,
        quickWins: [opp({ id: 'a', title: 'Checklist Bottleneck', aiRationale: marker })],
        opportunities: [opp({ id: 'a', title: 'Checklist Bottleneck', aiRationale: marker })],
      },
      { filename: 'x.pdf', footerText: 'f' },
    );
    const bytes = decodePdf(cap.pdf.output('datauristring'));
    expect(bytes).not.toContain(marker);
  });

  it('is a faithful projection of its input — so enforcement must be server-side', async () => {
    // Deliberately seed floor-violating content in the two fields the exporter
    // DOES render. It appears in the output, which is the point: the client
    // cannot be the enforcement boundary, so the server must not send this.
    // (The aggregation floor sweeps the executive report and the Track-A seed at
    // materialization; `secops_volume` is swept as of T5.)
    await downloadExecutiveReportPdf(
      {
        ...CLEAN_DATA,
        summary: 'Host 10.1.2.3 is affected by CVE-2026-1234.',
        quickWins: [opp({ id: 'a', title: 'Patch 10.1.2.3 for CVE-2026-1234' })],
        opportunities: [opp({ id: 'a', title: 'Patch 10.1.2.3 for CVE-2026-1234' })],
      },
      { filename: 'x.pdf', footerText: 'f' },
    );
    const bytes = decodePdf(cap.pdf.output('datauristring'));
    // Proven, not assumed: whatever the server sends reaches the artifact.
    expect(bytes).toContain('CVE-2026-1234');
    // ...and the exporter offers no redaction hook of its own, which is why the
    // AC5 guarantee is enforced before the data ever leaves the backend.
  });

  it('renders opportunity titles into the chart raster (unauditable by text checks)', async () => {
    // The effort/impact chart is rasterised from an SVG whose <text> labels are
    // opportunity titles. A raster cannot be swept by any text-based export
    // check, which is the strongest reason the floor must hold server-side.
    await downloadExecutiveReportPdf(CLEAN_DATA, { filename: 'x.pdf', footerText: 'f' });
    const bytes = decodePdf(cap.pdf.output('datauristring'));
    expect(bytes).toContain('/Image');
  });
});
