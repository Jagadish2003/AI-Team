// @vitest-environment jsdom
/**
 * 2.0-B1 T4 — the readable evidence PDF.
 *
 * jsPDF cannot be pixel-asserted in jsdom, so these tests pin what CAN be checked
 * without a renderer, and those happen to be the things most likely to break:
 *
 *   * it does not throw on a thin or malformed envelope. The bundle has already
 *     been signed and audited by the time we get here — the caller is entitled to
 *     their document, and a rendering crash would deny them one they already paid
 *     for;
 *   * every string the document must contain is passed to jsPDF's text API. That
 *     covers the banner (this is a rendering, not the verifiable artifact), the
 *     signature and content root, the incomplete-chain notice, and the per-hop
 *     AC1 fields;
 *   * the filename is sanitised, since run/opportunity ids reach it.
 *
 * jsPDF is stubbed rather than run: the point is what the module ASKS to be
 * drawn. Whether the glyphs land correctly is a visual check, and this file does
 * not pretend otherwise.
 *
 * Run: npx vitest run src/__tests__/evidenceReportPdf.test.ts
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const { drawn, saved, ctorArgs } = vi.hoisted(() => ({
  drawn: [] as string[],
  saved: [] as string[],
  ctorArgs: [] as unknown[],
}));

vi.mock('jspdf', () => {
  class FakePdf {
    constructor(opts: unknown) {
      ctorArgs.push(opts);
    }
    setFont() {}
    setFontSize() {}
    setTextColor() {}
    setFillColor() {}
    setDrawColor() {}
    line() {}
    roundedRect() {}
    addPage() {}
    setPage() {}
    getNumberOfPages() {
      return 1;
    }
    splitTextToSize(text: string) {
      return [text];
    }
    text(value: string) {
      drawn.push(value);
    }
    save(filename: string) {
      saved.push(filename);
    }
  }
  return { jsPDF: FakePdf };
});

import {
  downloadEvidenceReportPdf,
  evidenceReportFilename,
} from '../utils/evidenceReportPdf';

/** Everything the document asked to draw, as one searchable string. */
const page = () => drawn.join('\n');

beforeEach(() => {
  drawn.length = 0;
  saved.length = 0;
  ctorArgs.length = 0;
});
afterEach(() => vi.clearAllMocks());

function envelope(overrides: Record<string, unknown> = {}) {
  return {
    algorithm: 'HMAC-SHA256',
    signature: 'a91fdeadbeef0000',
    bundle: {
      scope: 'finding',
      run_id: 'run_42',
      opportunity_id: 'opp_7',
      generated_at: '2026-08-07T07:35:00+00:00',
      finding_count: 1,
      truncated: false,
      redacted_pattern_types: [],
      run_provenance: {
        started_at: '2026-08-07T07:00:00Z',
        completed_at: '2026-08-07T07:35:00Z',
        mode: 'offline',
        pack_id: 'service_cloud',
        pack_version: '1.2.0',
      },
      integrity: { content_root: '4c2ecafebabe1111' },
      findings: [
        {
          opportunity_id: 'opp_7',
          opportunity: {
            title: 'Automate cross-system record sync',
            confidence: 'MEDIUM',
            corroboration_label: 'Corroborated by ServiceNow incidents',
            packId: 'service_cloud',
            packVersion: '1.2.0',
          },
          trace: {
            complete: true,
            incomplete_reason: null,
            hops: [
              {
                hop_id: 'f1', hop_type: 'finding', label: 'Automate sync',
                origin: 'observed', connector: 'jira,servicenow',
                run_id: 'run_42', timestamp: '07 Aug 2026, 07:35', from_hop_id: null,
              },
              {
                hop_id: 'e1', hop_type: 'evidence', label: 'Ticket duplication',
                origin: 'observed', connector: 'servicenow',
                run_id: 'run_7', timestamp: '07 Aug 2026, 07:35', from_hop_id: 'f1',
              },
            ],
          },
          evidence: [
            { id: 'ev1', title: 'Ticket duplication', source: 'servicenow', tsLabel: '07 Aug 2026, 07:35' },
          ],
        },
      ],
      ...overrides,
    },
  } as never;
}

describe('the document says what it is', () => {
  it('states that it is a rendering, not the verifiable artifact', async () => {
    await downloadEvidenceReportPdf(envelope());
    expect(page()).toMatch(/not the verifiable artifact/i);
  });

  it('prints the signature and content root so it ties to the bundle by eye', async () => {
    await downloadEvidenceReportPdf(envelope());
    expect(page()).toContain('a91fdeadbeef0000');
    expect(page()).toContain('4c2ecafebabe1111');
    expect(page()).toContain('HMAC-SHA256');
  });

  it('points at the offline verifier', async () => {
    await downloadEvidenceReportPdf(envelope());
    expect(page()).toMatch(/verify_evidence_export\.py/);
    expect(page()).toMatch(/report_key/);
  });

  it('asserts no causation in the footer', async () => {
    await downloadEvidenceReportPdf(envelope());
    expect(page()).toMatch(/asserts no causation/i);
  });
});

describe('honesty carried into print', () => {
  it('prints the incomplete-chain notice when the chain stops short', async () => {
    const e = envelope() as never as { bundle: Record<string, unknown> };
    (e.bundle.findings as Record<string, unknown>[])[0].trace = {
      complete: false, incomplete_reason: 'no_source_record', hops: [],
    };
    await downloadEvidenceReportPdf(e as never);
    expect(page()).toMatch(/stops at the evidence layer/i);
  });

  it('prints nothing about incompleteness when the chain is complete', async () => {
    await downloadEvidenceReportPdf(envelope());
    expect(page()).not.toMatch(/stops at the evidence layer/i);
  });

  it('discloses truncation', async () => {
    await downloadEvidenceReportPdf(envelope({ truncated: true }));
    expect(page()).toMatch(/truncated/i);
  });

  it('discloses redaction and names the pattern types', async () => {
    await downloadEvidenceReportPdf(
      envelope({ redacted_pattern_types: ['aws_access_key', 'jwt'] }),
    );
    expect(page()).toMatch(/aws_access_key, jwt/);
  });

  it('prints each hop with its own run id (AC1)', async () => {
    await downloadEvidenceReportPdf(envelope());
    expect(page()).toMatch(/run run_42/);
    expect(page()).toMatch(/run run_7/);
  });

  it('says so when a finding has no evidence, rather than printing a gap', async () => {
    const e = envelope() as never as { bundle: Record<string, unknown> };
    (e.bundle.findings as Record<string, unknown>[])[0].evidence = [];
    await downloadEvidenceReportPdf(e as never);
    expect(page()).toMatch(/No evidence records are attached/i);
  });
});

describe('it never denies an already-signed artifact', () => {
  it.each([
    ['empty envelope', {}],
    ['no bundle body', { bundle: {} }],
    ['no findings', { bundle: { scope: 'finding', findings: [] } }],
    ['a finding with nothing on it', { bundle: { findings: [{}] } }],
    ['hops that are not objects', { bundle: { findings: [{ trace: { hops: [1, 2] } }] } }],
  ])('renders a degenerate envelope: %s', async (_label, env) => {
    await expect(downloadEvidenceReportPdf(env as never)).resolves.toBeUndefined();
    expect(saved).toHaveLength(1);
  });

  it('does not print the literal "undefined" or "null"', async () => {
    await downloadEvidenceReportPdf({ bundle: { scope: 'finding' } } as never);
    expect(page()).not.toMatch(/\bundefined\b/);
    expect(page()).not.toMatch(/\bnull\b/);
  });

  it('survives a cyclic chain rather than hanging', async () => {
    const e = envelope() as never as { bundle: Record<string, unknown> };
    (e.bundle.findings as Record<string, unknown>[])[0].trace = {
      complete: true,
      hops: [
        { hop_id: 'a', label: 'A', from_hop_id: 'b', hop_type: 'evidence', origin: 'observed', run_id: 'r' },
        { hop_id: 'b', label: 'B', from_hop_id: 'a', hop_type: 'evidence', origin: 'observed', run_id: 'r' },
      ],
    };
    await downloadEvidenceReportPdf(e as never);
    expect(page()).toContain('A');
    expect(page()).toContain('B');
  });
});

describe('filename', () => {
  it('names the scope, run and opportunity', () => {
    expect(evidenceReportFilename(envelope())).toBe(
      'agentiq-evidence-finding-run_42-opp_7.pdf',
    );
  });

  it('sanitises path and shell characters out of ids', () => {
    const name = evidenceReportFilename(
      envelope({ run_id: 'run/../../etc/passwd', opportunity_id: 'opp"; rm -rf /' }),
    );
    for (const bad of ['"', '/', '\\', ';', ' ']) expect(name).not.toContain(bad);
    expect(name.endsWith('.pdf')).toBe(true);
  });

  it('is what the document is saved as', async () => {
    await downloadEvidenceReportPdf(envelope());
    expect(saved).toEqual(['agentiq-evidence-finding-run_42-opp_7.pdf']);
  });
});

describe('page setup', () => {
  it('is A4 portrait in millimetres, matching the executive report', async () => {
    await downloadEvidenceReportPdf(envelope());
    expect(ctorArgs[0]).toMatchObject({ unit: 'mm', format: 'a4', orientation: 'portrait' });
  });
});
