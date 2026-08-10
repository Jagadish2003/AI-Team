/**
 * 2.0-B1 T4 — the READABLE half of the signed evidence export, as a PDF.
 *
 * Item 4 names three audiences: "auditors, regulators, **and board packs**". The
 * signed JSON serves the first two — canonical bytes plus the dependency-free
 * `scripts/verify_evidence_export.py` is exactly what a verifier wants. It serves
 * the third not at all; nobody reads a 400-line JSON envelope in a board meeting.
 *
 * So there are two downloads, not one file trying to be both:
 *
 *   - this PDF        — what a person reads.
 *   - the signed JSON — what an auditor verifies.
 *
 * **This PDF is a rendering. It is not the verifiable artifact**, and it says so
 * on its first page. If a reader hands the PDF to an auditor who then cannot
 * verify it, the integrity story is worse than if no PDF existed — hence the
 * banner, and hence the signature and content root printed here so the two files
 * can be tied together by eye.
 *
 * Built from the SIGNED envelope, never from run data. That is what makes AC5
 * hold for free: the aggregation floor and secret redaction run server-side
 * BEFORE signing, so anything reachable here has already passed them. Re-reading
 * the run to build a prettier document would create a second export surface with
 * its own discipline to get right — and a readable one, the worse of the two to
 * get wrong.
 *
 * Rendered client-side with jsPDF, already a dependency (`utils/exportPdf.ts`
 * builds the executive report the same way), so this adds no backend dependency
 * and no new signing scheme. Layout constants and the WinAnsi `sanitize` rule are
 * kept consistent with that module — same page geometry, same palette, same
 * non-Latin limitation.
 */
import type { EvidenceExportEnvelope } from '../api/evidenceExportApi';

type RGB = [number, number, number];

const NAVY: RGB = [7, 25, 58];
const BODY: RGB = [33, 45, 72];
const MUTED: RGB = [101, 116, 139];
const DIVIDER: RGB = [214, 222, 234];
const WARN_BG: RGB = [253, 246, 236];
const WARN_BORDER: RGB = [217, 180, 138];
const INFO_BG: RGB = [243, 246, 249];
const INFO_BORDER: RGB = [185, 196, 208];

const PW = 210;
const MX = 15;
const TOP = 15;
const BOTTOM = 278;
const CW = PW - MX * 2;
const RIGHT = PW - MX;

const PT_TO_MM = 0.3528;
const lineHeight = (pt: number) => pt * PT_TO_MM * 1.34;
const ascent = (pt: number) => pt * PT_TO_MM * 0.74;

// Same WinAnsi limitation as exportPdf.ts — jsPDF's standard fonts cannot encode
// non-Latin scripts, so they are stripped rather than rendered as missing-glyph
// boxes. Fixing it properly means embedding a Unicode TTF, deferred there too.
const STRIP_UNRENDERABLE = new RegExp(
  '[^\\x09\\x0A\\x0D\\x20-\\xFF\\u2010-\\u2027\\u2030-\\u205E]',
  'g',
);
function sanitize(s: unknown): string {
  return String(s ?? '').replace(STRIP_UNRENDERABLE, '');
}

/** A display string, or a dash. Never the literal "null"/"undefined". */
function text(value: unknown): string {
  const s = sanitize(value).trim();
  return s || '-';
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function asArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

/**
 * Depth of each hop, derived from `from_hop_id`, so the printed chain nests the
 * way the on-screen Source Trace panel does — a reviewer comparing the two
 * should not have to reconcile different shapes. Cycle-guarded: a malformed
 * chain must not stall the production of an audit artifact.
 */
function hopDepths(hops: Record<string, unknown>[]): Map<string, number> {
  const byId = new Map(hops.map((h) => [String(h.hop_id ?? ''), h]));
  const cache = new Map<string, number>();
  const depthOf = (id: string, guard = 0): number => {
    const cached = cache.get(id);
    if (cached !== undefined) return cached;
    const parent = String(byId.get(id)?.from_hop_id ?? '');
    const depth =
      !parent || !byId.has(parent) || guard > 32 ? 0 : depthOf(parent, guard + 1) + 1;
    cache.set(id, depth);
    return depth;
  };
  const depths = new Map<string, number>();
  hops.forEach((h) => {
    const id = String(h.hop_id ?? '');
    depths.set(id, depthOf(id));
  });
  return depths;
}

/**
 * The chain-incomplete sentence, or null when it terminates in source records.
 *
 * Carried into the PDF deliberately. A signed-looking document that quietly
 * stops above its source records reads as more authoritative than it is, and its
 * reader is exactly the person least able to notice.
 */
function completenessNote(trace: Record<string, unknown>): string | null {
  if (!Object.keys(trace).length) {
    return 'No provenance chain was recorded for this finding.';
  }
  if (trace.complete) return null;
  if (trace.incomplete_reason === 'no_source_record') {
    return 'This chain stops at the evidence layer - no originating source records were recorded for this run.';
  }
  return 'This chain does not reach its source records.';
}

/** One line naming every pack version the run stamped. */
function packSummary(provenance: Record<string, unknown>): string {
  const versions = asRecord(provenance.pack_versions);
  const entries = Object.entries(versions);
  if (entries.length) {
    return entries
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([k, v]) => `${k} ${String(v)}`)
      .join(', ');
  }
  if (provenance.pack_id) {
    return `${text(provenance.pack_id)} ${text(provenance.pack_version)}`;
  }
  return '-';
}

export function evidenceReportFilename(envelope: EvidenceExportEnvelope): string {
  const body = asRecord(envelope?.bundle);
  const scope = text(body.scope);
  const runId = text(body.run_id);
  const oppId = body.opportunity_id ? `-${text(body.opportunity_id)}` : '';
  const stem = `agentiq-evidence-${scope}-${runId}${oppId}`;
  return `${stem.replace(/[^A-Za-z0-9-_.]/g, '-')}.pdf`;
}

/**
 * Render the signed envelope as a readable PDF and hand it to the browser.
 *
 * Never throws on a thin or malformed envelope — the bundle has already been
 * signed and the caller is entitled to their document; missing sections render
 * as an explicit "none recorded" rather than a blank page or an exception.
 */
export async function downloadEvidenceReportPdf(
  envelope: EvidenceExportEnvelope,
): Promise<void> {
  const { jsPDF } = await import('jspdf');
  const pdf = new jsPDF({ unit: 'mm', format: 'a4', orientation: 'portrait' });

  const body = asRecord(envelope?.bundle);
  const provenance = asRecord(body.run_provenance);
  const integrity = asRecord(body.integrity);
  const scope = String(body.scope ?? 'export');
  const scopeLabel = scope === 'finding' ? 'Single finding' : 'Whole run (report scope)';

  let y = TOP;

  const setFont = (pt: number, style: 'normal' | 'bold', c: RGB) => {
    pdf.setFont('helvetica', style);
    pdf.setFontSize(pt);
    pdf.setTextColor(c[0], c[1], c[2]);
  };
  const ensure = (h: number) => {
    if (y + h > BOTTOM) {
      pdf.addPage();
      y = TOP;
    }
  };
  const line = (s: string, pt: number, style: 'normal' | 'bold', c: RGB, x = MX) => {
    const h = lineHeight(pt);
    ensure(h);
    setFont(pt, style, c);
    pdf.text(sanitize(s), x, y + ascent(pt));
    y += h;
  };
  const wrapped = (s: string, pt: number, style: 'normal' | 'bold', c: RGB, maxW = CW) => {
    setFont(pt, style, c);
    (pdf.splitTextToSize(sanitize(s), maxW) as string[]).forEach((ln) =>
      line(ln, pt, style, c),
    );
  };
  const heading = (s: string) => {
    y += 3;
    ensure(9);
    setFont(12, 'bold', NAVY);
    pdf.text(sanitize(s), MX, y + 4.4);
    y += 6.2;
    pdf.setDrawColor(DIVIDER[0], DIVIDER[1], DIVIDER[2]);
    pdf.line(MX, y, RIGHT, y);
    y += 3.5;
  };
  const field = (label: string, value: string) => {
    const pt = 9;
    const h = lineHeight(pt);
    ensure(h);
    setFont(pt, 'bold', MUTED);
    pdf.text(sanitize(label), MX, y + ascent(pt));
    setFont(pt, 'normal', BODY);
    (pdf.splitTextToSize(sanitize(value), CW - 42) as string[]).forEach((ln, i) => {
      if (i > 0) {
        y += h;
        ensure(h);
      }
      pdf.text(ln, MX + 42, y + ascent(pt));
    });
    y += h;
  };
  const callout = (title: string, message: string, warn = false) => {
    const pt = 8.5;
    setFont(pt, 'normal', BODY);
    const lines = pdf.splitTextToSize(sanitize(message), CW - 8) as string[];
    const boxH = 5 + lineHeight(pt) * (lines.length + 1) + 2;
    ensure(boxH + 2);
    const bg = warn ? WARN_BG : INFO_BG;
    const border = warn ? WARN_BORDER : INFO_BORDER;
    pdf.setFillColor(bg[0], bg[1], bg[2]);
    pdf.setDrawColor(border[0], border[1], border[2]);
    pdf.roundedRect(MX, y, CW, boxH, 1.5, 1.5, 'FD');
    let ty = y + 3;
    setFont(pt, 'bold', NAVY);
    pdf.text(sanitize(title), MX + 4, ty + ascent(pt));
    ty += lineHeight(pt);
    setFont(pt, 'normal', BODY);
    lines.forEach((ln) => {
      pdf.text(ln, MX + 4, ty + ascent(pt));
      ty += lineHeight(pt);
    });
    y += boxH + 3;
  };

  // ── Title ────────────────────────────────────────────────────────────────
  line('AgentIQ evidence export', 17, 'bold', NAVY);
  line(
    `${scopeLabel}  ·  run ${text(body.run_id)}  ·  generated ${text(body.generated_at)}`,
    8.5,
    'normal',
    MUTED,
  );
  y += 3;

  // The one thing this document must never let a reader get wrong.
  callout(
    'This document is a rendering. It is not the verifiable artifact.',
    'Verification is performed against the signed bundle (.json) downloaded separately - the canonical bytes the signature covers. Reformatting or editing that file, in any way, causes verification to fail. Verify it offline with scripts/verify_evidence_export.py and the installation\'s licence report_key.',
  );

  // ── Provenance ───────────────────────────────────────────────────────────
  heading('Provenance');
  field('Scope', scopeLabel);
  field('Run', text(body.run_id));
  field('Run started', text(provenance.started_at));
  field('Run completed', text(provenance.completed_at));
  field('Run mode', text(provenance.mode));
  field('Pack versions', packSummary(provenance));
  field('Findings in bundle', text(body.finding_count ?? asArray(body.findings).length));
  field('Signature', `${text(envelope?.algorithm)} ${text(envelope?.signature)}`);
  field('Content root', text(integrity.content_root));

  if (body.truncated) {
    callout(
      'This bundle is truncated.',
      'The run produced more findings than one bundle carries. The count above is what is included - it is reported here rather than presented as the whole run.',
      true,
    );
  }
  const redacted = asArray(body.redacted_pattern_types).map((p) => String(p));
  if (redacted.length) {
    callout(
      'Redaction applied before signing.',
      `Secret-shaped content of these types was removed from the exported material and is not recoverable from this document: ${redacted.join(', ')}.`,
      true,
    );
  }

  // ── Findings ─────────────────────────────────────────────────────────────
  heading('Findings');
  const findings = asArray(body.findings).map(asRecord);
  if (!findings.length) {
    wrapped('This bundle contains no findings.', 9, 'normal', MUTED);
  }

  findings.forEach((section, index) => {
    const opportunity = asRecord(section.opportunity);
    const trace = asRecord(section.trace);

    y += 2;
    wrapped(
      `${index + 1}. ${text(opportunity.title ?? section.opportunity_id)}`,
      10.5,
      'bold',
      NAVY,
    );
    field('Opportunity', text(section.opportunity_id));
    field('Confidence', text(opportunity.confidence));
    field('Corroboration', text(opportunity.corroboration_label));
    field('Pack', `${text(opportunity.packId)} ${text(opportunity.packVersion)}`.trim());

    const note = completenessNote(trace);
    if (note) callout('Chain is incomplete.', note, true);

    // Chain. Indentation mirrors the on-screen panel; each hop carries the four
    // fields AC1 names (origin, connector, run id, timestamp).
    const hops = asArray(trace.hops).map(asRecord);
    if (hops.length) {
      y += 1;
      line('Chain', 8.5, 'bold', MUTED);
      const depths = hopDepths(hops);
      hops.forEach((hop) => {
        const indent = '    '.repeat(depths.get(String(hop.hop_id ?? '')) ?? 0);
        wrapped(`${indent}${text(hop.label)}`, 8.5, 'normal', BODY);
        wrapped(
          `${indent}    ${text(hop.hop_type)} / ${text(hop.origin)} / ${text(
            hop.connector,
          )} / run ${text(hop.run_id)} / ${text(hop.timestamp)}`,
          7.5,
          'normal',
          MUTED,
        );
      });
    } else {
      wrapped('No provenance chain was recorded for this finding.', 8.5, 'normal', MUTED);
    }

    const evidence = asArray(section.evidence).map(asRecord);
    y += 1;
    line('Evidence', 8.5, 'bold', MUTED);
    if (evidence.length) {
      evidence.forEach((ev) => {
        wrapped(
          `- ${text(ev.title ?? ev.evidenceType ?? ev.id)}`,
          8.5,
          'normal',
          BODY,
        );
        wrapped(
          `      ${text(ev.source)} / ${text(ev.tsLabel)}`,
          7.5,
          'normal',
          MUTED,
        );
      });
    } else {
      wrapped('No evidence records are attached to this finding.', 8.5, 'normal', MUTED);
    }

    y += 2;
    ensure(2);
    pdf.setDrawColor(DIVIDER[0], DIVIDER[1], DIVIDER[2]);
    pdf.line(MX, y, RIGHT, y);
    y += 2;
  });

  // ── Footer on every page ─────────────────────────────────────────────────
  const pages = pdf.getNumberOfPages();
  for (let page = 1; page <= pages; page += 1) {
    pdf.setPage(page);
    setFont(7, 'normal', MUTED);
    pdf.text(
      sanitize(
        'Produced by AgentIQ. Evidence is reported as observed; this document asserts no causation. ' +
          'Verify the signed bundle (.json) before relying on any figure here.',
      ),
      MX,
      288,
    );
    pdf.text(`${page} / ${pages}`, RIGHT, 288, { align: 'right' });
  }

  pdf.save(evidenceReportFilename(envelope));
}
