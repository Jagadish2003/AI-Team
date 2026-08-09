/**
 * exportPdf — build a board-ready Executive Report PDF and trigger a download.
 *
 * The PDF is drawn with jsPDF's native text/vector API (NOT a rasterized
 * screenshot), so all body text is real, selectable, copy-pasteable text. The
 * document is always light, contains no navbar/buttons, and is laid out as a
 * clean multi-page A4 report suitable for leadership.
 *
 * The Effort vs Impact chart is rasterized from the SAME SVG the on-screen
 * chart renders (utils/matrixLayout.buildMatrixSvg) with the light palette, so
 * the embedded chart is visually identical to what the user sees in the app.
 * Only that chart figure is an image; all report text remains selectable.
 *
 * jsPDF is imported dynamically so it stays code-split out of the main bundle.
 */
import type { PackCertification } from '../types/packCertification';
import type { OpportunityCandidate } from '../types/analystReview';
import type { OutcomeReportSection } from '../types/outcome';
import { buildMatrixSvg, LIGHT_MATRIX_PALETTE } from './matrixLayout';
import { LEADERSHIP_ACTIONS } from '../components/executive_report/KeyInsights';
import { projectionBasisSummary } from '../components/projection/ProjectionBasis';
import { recommendationHeadline } from '../components/projection/ProjectionRecommendation';
import { showRelease2ArcAUi } from '../config/releaseFlags';
import {
  outcomeCaveatExplanation,
  outcomeCaveatLabel,
  outcomeCaveatSeverity,
} from './outcomeCaveats';

export interface ExecutiveReportPdfData {
  confidence: string;
  sourcesLabel: string;
  quickWinsCount: number;
  roadmapStageLabel: string;
  summary: string;
  quickWins: OpportunityCandidate[];
  stageCounts: number[];
  blockerCount: number;
  overallReadiness: string;
  opportunities: OpportunityCandidate[];
  orgName?: string | null;
  /** Display name of the signed-in user (e.g. "Likhith"). Rendered as
   * "<userName>'s Profile" in the header, with orgName on the line above. */
  userName?: string | null;
  generatedAt: string;
  runId?: string | null;
  /**
   * 2.0-C2 T3 (AT-833 / AC2): the certification level of every pack that produced
   * a claim in this report, in order of first appearance. Rendered under the
   * title so a board paper says which level of pack produced its claims.
   */
  packCertifications?: PackCertification[];
  outcomeSection?: OutcomeReportSection | null;
}

export interface PdfExportOptions {
  filename: string;
  footerText?: string;
}

type RGB = [number, number, number];

// Palette (light, board-ready).
const NAVY: RGB = [7, 25, 58];
const BODY: RGB = [33, 45, 72];
const MUTED: RGB = [101, 116, 139];
const ACCENT: RGB = [13, 85, 215];
const DIVIDER: RGB = [214, 222, 234];
const CARD_BORDER: RGB = [214, 222, 234];
const WHITE: RGB = [255, 255, 255];

// A4 portrait, millimetres.
const PW = 210;
const PH = 297;
const MX = 15;
const TOP = 15;
const BOTTOM = 278;
const CW = PW - MX * 2;
const RIGHT = PW - MX;

// Chart virtual size + its fixed height once scaled to the content width. The
// chart spans the full content width (aligned with the heading above it) and is
// rendered tall enough to be comfortably readable on the page.
const CHART_VW = 1440;
const CHART_VH = 900;
const CHART_H = (CHART_VH * CW) / CHART_VW; // ≈ 112.5mm at 180mm content width
// Tighter plot insets than the on-screen defaults so the chart box reaches
// closer to the page edges (uses the available left/right space). The left
// gutter still holds the impact axis labels.
const CHART_MARGINS = { left: 112, right: 24, top: 22, bottom: 48 };

const PT_TO_MM = 0.3528;
const lineHeight = (pt: number) => pt * PT_TO_MM * 1.34;
const ascent = (pt: number) => pt * PT_TO_MM * 0.74;

// Drop characters jsPDF's standard (WinAnsi) fonts can't encode — keeps Latin-1
// (\x20-\xFF) plus common typographic punctuation (U+2010–U+2027, U+2030–U+205E:
// dashes, smart quotes, bullet, ellipsis), and strips stray CJK/other glyphs from
// upstream data so they don't render as missing-glyph boxes in the PDF.
//
// LIMITATION: this drops non-Latin scripts (CJK, Arabic, Cyrillic beyond Latin-1,
// etc.) from PDF *text* (org name, opportunity titles, summary). The standard
// jsPDF fonts are WinAnsi-only, so the proper fix is embedding a Unicode TTF
// (e.g. Noto Sans via addFileToVFS/addFont) — deferred as it adds a large font
// asset. NOTE: the Effort vs Impact chart is exempt — it's rasterized from a
// browser-rendered SVG, which renders any script the system has a font for.
// TODO(executive-report): embed a Unicode font to support non-Latin report text.
const STRIP_UNRENDERABLE = new RegExp(
  '[^\\x09\\x0A\\x0D\\x20-\\xFF\\u2010-\\u2027\\u2030-\\u205E]',
  'g',
);
function sanitize(s: string | null | undefined): string {
  return (s ?? '').replace(STRIP_UNRENDERABLE, '');
}

/**
 * Rasterize an SVG string to a PNG data URL via an offscreen canvas. Returns
 * null when canvas isn't available (e.g. jsdom in tests) so callers can fall
 * back gracefully.
 */
async function rasterizeSvgToPng(svg: string, pxW: number, pxH: number): Promise<string | null> {
  try {
    const canvas = document.createElement('canvas');
    const ctx = canvas.getContext('2d');
    if (!ctx) return null;
    canvas.width = pxW;
    canvas.height = pxH;
    const uri = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(svg);
    const img = new Image();
    await new Promise<void>((resolve, reject) => {
      img.onload = () => resolve();
      img.onerror = () => reject(new Error('svg rasterize failed'));
      img.src = uri;
    });
    ctx.drawImage(img, 0, 0, pxW, pxH);
    return canvas.toDataURL('image/png');
  } catch {
    return null;
  }
}

/**
 * Wrap a KPI value. For " / "-separated values (the Agent Roadmap label,
 * "Phase 1 / Phase 2 / Phase 3"), each token is kept intact on its line so a
 * "Phase" is never split from its number; the separator stays with the
 * preceding token. Plain values fall through to splitTextToSize.
 */
function wrapKpiValue(pdf: import('jspdf').jsPDF, value: string, maxW: number): string[] {
  if (!value.includes(' / ')) {
    return pdf.splitTextToSize(value, maxW) as string[];
  }
  const tokens = value.split(' / ');
  const lines: string[] = [];
  let cur = '';
  tokens.forEach((tok, i) => {
    const piece = i < tokens.length - 1 ? `${tok} /` : tok;
    const trial = cur ? `${cur} ${piece}` : piece;
    if (!cur || pdf.getTextWidth(trial) <= maxW) {
      cur = trial;
    } else {
      lines.push(cur);
      cur = piece;
    }
  });
  if (cur) lines.push(cur);
  return lines;
}

function formatOutcomePdfNumber(value: number | null | undefined, unit?: string | null): string {
  if (value === null || value === undefined || Number.isNaN(Number(value))) {
    return 'Unavailable';
  }
  const formatted = new Intl.NumberFormat('en-US', {
    maximumFractionDigits: unit === 'percent' ? 1 : 2,
  }).format(Number(value));
  return unit === 'percent' ? `${formatted}%` : formatted;
}

/** Rasterize the bundled AgentIQ logo SVG to a PNG data URL for embedding.
 *  Returns null on any failure so the report still renders without the logo. */
async function loadLogoPng(): Promise<{ dataUrl: string; w: number; h: number } | null> {
  try {
    const res = await fetch('/Logo-Light.svg');
    if (!res.ok) return null;
    const svg = await res.text();
    const dataUri = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(svg);
    const img = new Image();
    await new Promise<void>((resolve, reject) => {
      img.onload = () => resolve();
      img.onerror = () => reject(new Error('logo load failed'));
      img.src = dataUri;
    });
    const baseW = img.naturalWidth || 300;
    const baseH = img.naturalHeight || 102;
    const scale = 4; // hi-res raster for a crisp logo
    const canvas = document.createElement('canvas');
    canvas.width = baseW * scale;
    canvas.height = baseH * scale;
    const ctx = canvas.getContext('2d');
    if (!ctx) return null;
    ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
    return { dataUrl: canvas.toDataURL('image/png'), w: baseW, h: baseH };
  } catch {
    return null;
  }
}

export async function downloadExecutiveReportPdf(
  data: ExecutiveReportPdfData,
  options: PdfExportOptions,
): Promise<void> {
  const { jsPDF } = await import('jspdf');
  const logo = await loadLogoPng();

  const pdf = new jsPDF({ unit: 'mm', format: 'a4', orientation: 'portrait' });
  let y = TOP;

  const setFill = (c: RGB) => pdf.setFillColor(c[0], c[1], c[2]);
  const setDraw = (c: RGB) => pdf.setDrawColor(c[0], c[1], c[2]);
  const setTxt = (c: RGB) => pdf.setTextColor(c[0], c[1], c[2]);
  const setFont = (pt: number, style: 'normal' | 'bold', c: RGB) => {
    pdf.setFont('helvetica', style);
    pdf.setFontSize(pt);
    setTxt(c);
  };
  const ensure = (h: number) => {
    if (y + h > BOTTOM) {
      pdf.addPage();
      y = TOP;
    }
  };
  // Draw a left-aligned text line at the cursor, advancing y by one line.
  const textLine = (
    text: string,
    pt: number,
    style: 'normal' | 'bold',
    c: RGB,
  ) => {
    const h = lineHeight(pt);
    ensure(h);
    setFont(pt, style, c);
    pdf.text(sanitize(text), MX, y + ascent(pt));
    y += h;
  };
  const wrapped = (text: string, pt: number, style: 'normal' | 'bold', c: RGB, maxW = CW) => {
    setFont(pt, style, c);
    const lines = pdf.splitTextToSize(sanitize(text), maxW) as string[];
    lines.forEach((ln) => textLine(ln, pt, style, c));
  };
  const heading = (text: string, caption?: string) => {
    ensure(9);
    setFont(12.5, 'bold', NAVY);
    pdf.text(sanitize(text), MX, y + 4.6);
    if (caption) {
      setFont(7.5, 'normal', MUTED);
      pdf.text(sanitize(caption), RIGHT, y + 4.6, { align: 'right' });
    }
    y += 9;
  };

  // ── Header: logo (left) + confidential block (right) ──────────────────────
  let logoBottom = TOP;
  if (logo) {
    const logoH = 11;
    const logoW = (logoH * logo.w) / logo.h;
    pdf.addImage(logo.dataUrl, 'PNG', MX, TOP, logoW, logoH);
    logoBottom = TOP + logoH;
  } else {
    setFont(17, 'bold', NAVY);
    pdf.text('Agent', MX, TOP + 8);
    const w = pdf.getTextWidth('Agent');
    setFont(17, 'bold', ACCENT);
    pdf.text('IQ', MX + w, TOP + 8);
    logoBottom = TOP + 10;
  }

  let ry = TOP + 3;
  setFont(8, 'bold', ACCENT);
  pdf.text('CONFIDENTIAL', RIGHT, ry, { align: 'right' });
  ry += 4.6;
  // Org name (line above) + "<user>'s Profile" (line below). There are multiple
  // orgs and users, so the header identifies both: which org this report belongs
  // to, and whose profile generated it. Each line is sanitized independently and
  // only rendered when something renderable remains — avoids a broken "'s
  // Profile" with an empty name when the name is entirely non-Latin (see
  // sanitize() limitation above).
  const safeOrgName = sanitize(data.orgName ?? '').trim();
  if (safeOrgName) {
    setFont(8.5, 'normal', MUTED);
    pdf.text(safeOrgName, RIGHT, ry, { align: 'right' });
    ry += 4.4;
  }
  const safeUserName = sanitize(data.userName ?? '').trim();
  if (safeUserName) {
    setFont(9, 'bold', NAVY);
    pdf.text(`${safeUserName}'s Profile`, RIGHT, ry, { align: 'right' });
    ry += 4.4;
  }
  setFont(8.5, 'normal', MUTED);
  pdf.text(sanitize(`Date: ${data.generatedAt}`), RIGHT, ry, { align: 'right' });
  ry += 4;
  if (data.runId) {
    pdf.text(sanitize(`Run · ${data.runId.slice(0, 8)}`), RIGHT, ry, { align: 'right' });
    ry += 4;
  }

  y = Math.max(logoBottom, ry) + 9;

  // ── Title + subtitle ──────────────────────────────────────────────────────
  textLine('Executive Report', 22, 'bold', NAVY);
  y += 1.5;
  wrapped(
    'Board-ready summary of source coverage, confidence, opportunity value, and implementation readiness.',
    10.5,
    'normal',
    MUTED,
  );
  // 2.0-C2 T3 (AT-833 / AC2): provenance of the CLAIMS, not of the document —
  // stated on the export itself so a reader quoting a finding downstream can say
  // which level of pack produced it without going back to the product.
  const certifications = data.packCertifications ?? [];
  if (certifications.length > 0) {
    wrapped(
      `Produced by: ${certifications
        .map(
          (item) =>
            `${item.packId} (${item.label}${item.reviewDue ? ', review due' : ''})`,
        )
        .join(' · ')}`,
      9,
      'normal',
      MUTED,
    );
  }
  y += 3.5;
  setDraw(ACCENT);
  pdf.setLineWidth(0.6);
  pdf.line(MX, y, RIGHT, y);
  y += 7;

  // ── KPI cards ──────────────────────────────────────────────────────────────
  const stats: Array<[string, string]> = [
    ['OVERALL CONFIDENCE', data.confidence],
    ['SOURCES ANALYZED', data.sourcesLabel],
    ['TOP OPPORTUNITIES', `${data.quickWinsCount} Quick Wins`],
    ['AGENT ROADMAP', data.roadmapStageLabel],
  ];
  const gap = 4;
  const cardW = (CW - gap * 3) / 4;
  setFont(12.5, 'bold', NAVY);
  let maxValLines = 1;
  const valLines = stats.map(([, v]) => {
    const lines = wrapKpiValue(pdf, sanitize(v), cardW - 6);
    maxValLines = Math.max(maxValLines, lines.length);
    return lines;
  });
  const cardH = 8 + maxValLines * lineHeight(12.5);
  ensure(cardH + 2);
  stats.forEach(([label], i) => {
    const x = MX + i * (cardW + gap);
    setFill(WHITE);
    setDraw(CARD_BORDER);
    pdf.setLineWidth(0.3);
    pdf.roundedRect(x, y, cardW, cardH, 2, 2, 'FD');
    setFont(7.5, 'bold', MUTED);
    pdf.text(label, x + 3.5, y + 5);
    setFont(12.5, 'bold', NAVY);
    valLines[i].forEach((ln, li) => pdf.text(ln, x + 3.5, y + 11 + li * lineHeight(12.5)));
  });
  y += cardH + 9;

  // ── Key Insights ────────────────────────────────────────────────────────────
  heading('Key Insights');
  wrapped(data.summary, 10, 'normal', BODY);
  y += 4;

  // Leadership box (border-only; drawn after content using the measured extent).
  const boxPad = 5;
  const bulletIndent = 6;
  const innerW = CW - boxPad * 2 - bulletIndent;
  const lh95 = lineHeight(9.5);
  // Pre-measure so the whole box can move to the next page if it won't fit.
  let estH = boxPad + lineHeight(9) + 2;
  const actionLineSets = LEADERSHIP_ACTIONS.map((a) => {
    pdf.setFont('helvetica', 'normal');
    pdf.setFontSize(9.5);
    return pdf.splitTextToSize(sanitize(a), innerW) as string[];
  });
  actionLineSets.forEach((ls) => {
    estH += ls.length * lh95 + 2;
  });
  estH += boxPad;
  ensure(estH);

  const boxTop = y;
  let c = boxTop + boxPad + ascent(9);
  setFont(9, 'bold', NAVY);
  pdf.text('WHAT LEADERSHIP SHOULD DO NEXT', MX + boxPad, c);
  c += lineHeight(9) + 1.5;
  LEADERSHIP_ACTIONS.forEach((a, ai) => {
    const ls = actionLineSets[ai];
    setFill(ACCENT);
    pdf.circle(MX + boxPad + 1.1, c - 1.4, 0.8, 'F');
    setFont(9.5, 'normal', BODY);
    ls.forEach((ln, li) => pdf.text(ln, MX + boxPad + bulletIndent, c + li * lh95));
    c += ls.length * lh95 + 2;
  });
  const boxBottom = c - 2 + boxPad;
  setDraw(CARD_BORDER);
  pdf.setLineWidth(0.3);
  pdf.roundedRect(MX, boxTop, CW, boxBottom - boxTop, 2.5, 2.5, 'S');
  y = boxBottom + 9;

  // -- Outcome Movement ----------------------------------------------------
  if (data.outcomeSection) {
    heading('Outcome Movement', 'Stored movement records');
    wrapped(data.outcomeSection.summary, 9.5, 'normal', BODY);
    y += 2;

    const aggregates = data.outcomeSection.aggregates;
    [
      `Actioned opportunities: ${aggregates.actionedOpportunityCount}.`,
      `Measured opportunities: ${aggregates.measuredOpportunityCount}.`,
      `Stored movement measurements: ${aggregates.measurementCount}.`,
      `Measurements carrying caveats: ${aggregates.caveatedMeasurementCount}.`,
    ].forEach((line) => wrapped(line, 9.5, 'normal', BODY));

    const runIds = Array.from(
      new Set(
        (data.outcomeSection.numberRefs ?? [])
          .flatMap((ref) => ref.evidence?.runIds ?? [])
          .filter(Boolean),
      ),
    ).slice(0, 8);
    if (runIds.length > 0) {
      wrapped(`Run ids in aggregate evidence: ${runIds.join(', ')}.`, 8.5, 'normal', MUTED);
    }

    data.outcomeSection.highlights.slice(0, 3).forEach((measurement) => {
      const primary = measurement.primaryMovement;
      const pct =
        primary?.deltaPct !== null && primary?.deltaPct !== undefined
          ? `${formatOutcomePdfNumber(primary.deltaPct, 'percent')} against baseline`
          : 'against baseline';
      wrapped(
        [
          primary?.signalName ?? 'primary signal',
          `movement ${formatOutcomePdfNumber(primary?.delta)}`,
          pct,
          `runs ${measurement.baselineRunId ?? 'n/a'} -> ${measurement.currentRunId ?? 'n/a'}`,
          `projection ${measurement.projectionValidation?.verdict ?? 'unknown'}`,
          `caveats ${measurement.confounderSummary?.count ?? 0}`,
        ].join(' - ') + '.',
        8.5,
        'normal',
        MUTED,
      );
      (measurement.confounders ?? []).forEach((caveat) => {
        const explanation = outcomeCaveatExplanation(caveat);
        wrapped(
          [
            `Caveat (${outcomeCaveatSeverity(caveat)}): ${outcomeCaveatLabel(caveat)}`,
            explanation ? `Why this matters: ${explanation}` : null,
          ]
            .filter(Boolean)
            .join('. ') + '.',
          8.5,
          'normal',
          MUTED,
        );
      });
    });
    y += 5;
  }

  // ── Top Quick Wins (no numbering) ────────────────────────────────────────────
  heading('Top Quick Wins');
  if (data.quickWins.length === 0) {
    wrapped('No quick wins identified for this discovery run.', 9.5, 'normal', MUTED);
  } else {
    data.quickWins.forEach((o) => {
      const basisSummary = showRelease2ArcAUi ? projectionBasisSummary(o.projection) : null;
      // 2.0-A1 T5: the export carries the same intervention-language statement
      // the screens show. AC3 covers exports explicitly, and a PDF is the
      // artefact most likely to be quoted in a board paper.
      const recommendation = showRelease2ArcAUi ? recommendationHeadline(o.projection) : null;
      const recommendationLines = recommendation
        ? (pdf.splitTextToSize(sanitize(recommendation), CW - 10) as string[])
        : [];
      const basisLines = basisSummary
        ? (pdf.splitTextToSize(sanitize(basisSummary), CW - 10) as string[])
        : [];
      const extraH =
        (recommendationLines.length + basisLines.length) *
          lineHeight(8) +
        2;
      const qcH = 14 + extraH;
      ensure(qcH + 3);
      setDraw(CARD_BORDER);
      pdf.setLineWidth(0.3);
      pdf.roundedRect(MX, y, CW, qcH, 2, 2, 'S');
      setFont(11, 'bold', NAVY);
      pdf.text(sanitize(o.title), MX + 5, y + 6);
      setFont(9, 'normal', MUTED);
      pdf.text(
        sanitize(`${o.category} · Impact ${o.impact}/10 · Effort ${o.effort}/10`),
        MX + 5,
        y + 11,
      );
      let basisY = y + 15.5;
      if (recommendationLines.length > 0) {
        setFont(8, 'normal', NAVY);
        recommendationLines.forEach((ln) => {
          pdf.text(ln, MX + 5, basisY);
          basisY += lineHeight(8);
        });
      }
      if (basisLines.length > 0) {
        setFont(8, 'normal', MUTED);
        basisLines.forEach((ln) => {
          pdf.text(ln, MX + 5, basisY);
          basisY += lineHeight(8);
        });
      }
      y += qcH + 3;
    });
  }
  y += 6;

  // ── Agent Roadmap Highlights ─────────────────────────────────────────────────
  heading('Agent Roadmap Highlights');
  const roadmapLines: string[] = [
    `${data.stageCounts[0] ?? 0} opportunities planned for Phase 1.`,
    `${data.stageCounts[1] ?? 0} opportunities planned for Phase 2.`,
    `${data.stageCounts[2] ?? 0} opportunities planned for Phase 3.`,
  ];
  if (data.blockerCount > 0) {
    roadmapLines.push(
      `${data.blockerCount} required data permission${data.blockerCount > 1 ? 's' : ''} still missing — resolve before pilots start.`,
    );
  }
  roadmapLines.push(`Overall readiness: ${data.overallReadiness}.`);
  const lh10 = lineHeight(10);
  roadmapLines.forEach((t) => {
    setFont(10, 'normal', BODY);
    const ls = pdf.splitTextToSize(sanitize(t), CW - 6) as string[];
    ensure(ls.length * lh10 + 2);
    setFill(ACCENT);
    pdf.circle(MX + 1.1, y + 1.5, 0.8, 'F');
    setFont(10, 'normal', BODY);
    ls.forEach((ln, li) => pdf.text(ln, MX + 6, y + 2.6 + li * lh10));
    y += ls.length * lh10 + 2.5;
  });
  y += 7;

  // ── Effort vs Impact (rasterized from the same SVG the app renders) ──────────
  ensure(9 + CHART_H + 2); // keep heading + chart together on one page
  heading('Effort vs Impact', 'Read-only opportunity snapshot');
  const chartSvg = buildMatrixSvg(data.opportunities, CHART_VW, CHART_VH, LIGHT_MATRIX_PALETTE, CHART_MARGINS);
  const chartPng = await rasterizeSvgToPng(chartSvg, CHART_VW * 2, CHART_VH * 2);
  if (chartPng) {
    pdf.addImage(chartPng, 'PNG', MX, y, CW, CHART_H);
  } else {
    // Canvas unavailable (e.g. headless tests): leave a light placeholder box.
    setDraw(CARD_BORDER);
    pdf.setLineWidth(0.3);
    pdf.roundedRect(MX, y, CW, CHART_H, 2, 2, 'S');
  }
  y += CHART_H + 6;

  // ── Footers on every page ─────────────────────────────────────────────────────
  const pageCount = pdf.getNumberOfPages();
  for (let i = 1; i <= pageCount; i += 1) {
    pdf.setPage(i);
    setDraw(DIVIDER);
    pdf.setLineWidth(0.2);
    pdf.line(MX, PH - 12, RIGHT, PH - 12);
    pdf.setFont('helvetica', 'normal');
    pdf.setFontSize(8);
    setTxt(MUTED);
    if (options.footerText) pdf.text(sanitize(options.footerText), MX, PH - 8);
    pdf.text(`Page ${i} of ${pageCount}`, RIGHT, PH - 8, { align: 'right' });
  }

  pdf.save(options.filename);
}
