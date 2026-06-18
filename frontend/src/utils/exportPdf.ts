/**
 * exportPdf — build a board-ready Executive Report PDF and trigger a download.
 *
 * The PDF is drawn with jsPDF's native text/vector API (NOT a rasterized
 * screenshot), so all body text is real, selectable, copy-pasteable text. The
 * document is always light, contains no navbar/buttons, and is laid out as a
 * clean multi-page A4 report suitable for leadership.
 *
 * The Effort vs Impact chart is drawn as vectors using the SAME geometry as the
 * on-screen chart (utils/matrixLayout), so the downloaded chart matches the app.
 *
 * jsPDF is imported dynamically so it stays code-split out of the main bundle.
 */
import type { OpportunityCandidate } from '../types/analystReview';
import {
  clamp,
  computeMatrixGeometry,
  DEFAULT_MATRIX_HEIGHT,
  DEFAULT_MATRIX_WIDTH,
} from './matrixLayout';
import { LEADERSHIP_ACTIONS } from '../components/executive_report/KeyInsights';

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
  generatedAt: string;
  runId?: string | null;
}

export interface PdfExportOptions {
  filename: string;
  footerText?: string;
}

type RGB = [number, number, number];

// Palette (light, board-ready). Chart colors are the light theme values
// pre-blended onto white since jsPDF fills are opaque.
const NAVY: RGB = [7, 25, 58];
const BODY: RGB = [33, 45, 72];
const MUTED: RGB = [101, 116, 139];
const ACCENT: RGB = [13, 85, 215];
const DIVIDER: RGB = [214, 222, 234];
const CARD_BORDER: RGB = [214, 222, 234];
const WHITE: RGB = [255, 255, 255];
const CH_QUAD_PRIMARY: RGB = [236, 241, 252];
const CH_GRID: RGB = [183, 194, 211];
const CH_AXIS: RGB = [64, 78, 105];
const CH_QLABEL_STRONG: RGB = [40, 55, 82];
const CH_QLABEL_MID: RGB = [60, 76, 104];
const CH_QLABEL_MUTED: RGB = [92, 106, 130];
const CH_BUBBLE_FILL: RGB = [206, 215, 229];
const CH_BUBBLE_STROKE: RGB = [138, 155, 179];
const CH_PILL_BORDER: RGB = [182, 204, 243];
const CH_PILL_TEXT: RGB = [7, 25, 58];

// A4 portrait, millimetres.
const PW = 210;
const PH = 297;
const MX = 15;
const TOP = 15;
const BOTTOM = 278;
const CW = PW - MX * 2;
const RIGHT = PW - MX;

const PT_TO_MM = 0.3528;
const lineHeight = (pt: number) => pt * PT_TO_MM * 1.34;
const ascent = (pt: number) => pt * PT_TO_MM * 0.74;

// Drop characters jsPDF's standard (WinAnsi) fonts can't encode — keeps Latin-1
// (\x20-\xFF) plus common typographic punctuation (U+2010–U+2027, U+2030–U+205E:
// dashes, smart quotes, bullet, ellipsis), and strips stray CJK/other glyphs from
// upstream data so they don't render as missing-glyph boxes in the PDF.
const STRIP_UNRENDERABLE = new RegExp(
  '[^\\x09\\x0A\\x0D\\x20-\\xFF\\u2010-\\u2027\\u2030-\\u205E]',
  'g',
);
function sanitize(s: string | null | undefined): string {
  return (s ?? '').replace(STRIP_UNRENDERABLE, '');
}

// The vector chart's height is constant (virtual aspect scaled to content width).
const CHART_H = (DEFAULT_MATRIX_HEIGHT * CW) / DEFAULT_MATRIX_WIDTH;

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
      setFont(8.5, 'normal', MUTED);
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
  setFont(8.5, 'normal', MUTED);
  if (data.orgName) {
    pdf.text(sanitize(`${data.orgName} profile`), RIGHT, ry, { align: 'right' });
    ry += 4;
  }
  pdf.text(sanitize(`Generated ${data.generatedAt}`), RIGHT, ry, { align: 'right' });
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
    const lines = pdf.splitTextToSize(sanitize(v), cardW - 6) as string[];
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

  // ── Top Quick Wins (no numbering) ────────────────────────────────────────────
  heading('Top Quick Wins');
  if (data.quickWins.length === 0) {
    wrapped('No quick wins identified for this discovery run.', 9.5, 'normal', MUTED);
  } else {
    data.quickWins.forEach((o) => {
      const qcH = 14;
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

  // ── Effort vs Impact (vector chart, same geometry as the app) ────────────────
  ensure(9 + CHART_H + 2); // keep heading + chart together on one page
  heading('Effort vs Impact', 'Read-only opportunity snapshot');
  drawMatrix(pdf, data.opportunities, y, { setFill, setDraw, setFont });
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

interface DrawHelpers {
  setFill: (c: RGB) => void;
  setDraw: (c: RGB) => void;
  setFont: (pt: number, style: 'normal' | 'bold', c: RGB) => void;
}

/**
 * Draw the Effort vs Impact matrix as PDF vectors at the given top-y. Uses the
 * shared matrix geometry (computed at the app's default virtual size) scaled to
 * the page content width, so it mirrors the on-screen chart. The caller is
 * responsible for reserving CHART_H of vertical space before calling.
 */
function drawMatrix(
  pdf: import('jspdf').jsPDF,
  opportunities: OpportunityCandidate[],
  topY: number,
  h: DrawHelpers,
): void {
  const VW = DEFAULT_MATRIX_WIDTH;
  const { layout, points, placements } = computeMatrixGeometry(opportunities, VW, DEFAULT_MATRIX_HEIGHT);
  const s = CW / VW; // mm per virtual px
  const ox = MX;
  const oy = topY;
  const mx = (px: number) => ox + px * s;
  const my = (py: number) => oy + py * s;

  // Quadrant tint (top-left "Quick Wins").
  h.setFill(CH_QUAD_PRIMARY);
  pdf.rect(mx(layout.left), my(layout.top), (layout.cx - layout.left) * s, (layout.cy - layout.top) * s, 'F');

  // Plot border + center cross.
  h.setDraw(CH_GRID);
  pdf.setLineWidth(0.2);
  pdf.rect(mx(layout.left), my(layout.top), (layout.rx - layout.left) * s, (layout.by - layout.top) * s, 'S');
  pdf.line(mx(layout.cx), my(layout.top), mx(layout.cx), my(layout.by));
  pdf.line(mx(layout.left), my(layout.cy), mx(layout.rx), my(layout.cy));

  // Axis labels.
  h.setFont(6.5, 'bold', CH_AXIS);
  pdf.text('HIGH IMPACT', mx(layout.left - 10), my(layout.top + 18), { align: 'right' });
  pdf.text('LOW IMPACT', mx(layout.left - 10), my(layout.by - 6), { align: 'right' });
  pdf.text('LOW EFFORT', mx(layout.left), my(layout.height - 10));
  pdf.text('HIGH EFFORT', mx(layout.rx), my(layout.height - 10), { align: 'right' });

  // Quadrant labels.
  h.setFont(7, 'bold', CH_QLABEL_STRONG);
  pdf.text('QUICK WINS', mx(layout.left + 14), my(layout.top + 24));
  h.setFont(7, 'bold', CH_QLABEL_MID);
  pdf.text('HIGH VALUE', mx(layout.cx + 14), my(layout.top + 24));
  h.setFont(7, 'bold', CH_QLABEL_MUTED);
  pdf.text('FOUNDATION', mx(layout.left + 14), my(layout.cy + 24));
  pdf.text('LONG TERM', mx(layout.cx + 14), my(layout.cy + 24));

  // Bubbles.
  h.setFill(CH_BUBBLE_FILL);
  h.setDraw(CH_BUBBLE_STROKE);
  pdf.setLineWidth(0.25);
  points.forEach((p) => pdf.circle(mx(p.x), my(p.y), p.r * s, 'FD'));

  // Non-overlapping labels: leader line + pill above the bubble.
  placements
    .filter((l) => !l.onBubble)
    .forEach((lab) => {
      const originX = clamp(lab.bubbleX, lab.centerX - lab.width / 2, lab.centerX + lab.width / 2);
      h.setDraw(CH_PILL_BORDER);
      pdf.setLineWidth(0.2);
      pdf.line(mx(originX), my(lab.pillBottom), mx(lab.bubbleX), my(lab.bubbleTop));

      h.setFont(6.5, 'normal', CH_PILL_TEXT);
      const tw = pdf.getTextWidth(sanitize(lab.title));
      const pillW = tw + 3.2;
      const pillH = 4.2;
      const pcx = mx(lab.centerX);
      const pillTop = my(lab.pillBottom) - pillH;
      h.setFill(WHITE);
      h.setDraw(CH_PILL_BORDER);
      pdf.setLineWidth(0.2);
      pdf.roundedRect(pcx - pillW / 2, pillTop, pillW, pillH, 1, 1, 'FD');
      pdf.text(sanitize(lab.title), pcx, pillTop + pillH - 1.4, { align: 'center' });
    });

  // Overlapping labels: pill drawn on the bubble.
  placements
    .filter((l) => l.onBubble)
    .forEach((lab) => {
      h.setFont(6, 'normal', CH_PILL_TEXT);
      const tw = pdf.getTextWidth(sanitize(lab.title));
      const pillW = tw + 3;
      const pillH = 4;
      const pcx = mx(lab.onX);
      const pcy = my(lab.onY);
      h.setFill(WHITE);
      h.setDraw(CH_PILL_BORDER);
      pdf.setLineWidth(0.2);
      pdf.roundedRect(pcx - pillW / 2, pcy - pillH / 2, pillW, pillH, 1, 1, 'FD');
      pdf.text(sanitize(lab.title), pcx, pcy + 1.4, { align: 'center' });
    });
}
