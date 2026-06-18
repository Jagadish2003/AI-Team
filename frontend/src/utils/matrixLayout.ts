/**
 * matrixLayout — pure geometry for the Effort vs Impact "snapshot matrix".
 *
 * Extracted from SnapshotMatrix so the same layout drives two renderers:
 *   - the on-screen SVG chart (SnapshotMatrix.tsx)
 *   - the PDF export's vector chart (utils/exportPdf.ts)
 *
 * Keeping the math in one place means the downloaded report's chart matches
 * what the user sees in the app. No DOM or React here — just numbers in / out.
 */
import type { OpportunityCandidate } from '../types/analystReview';

export function clamp(n: number, a: number, b: number): number {
  return Math.max(a, Math.min(b, n));
}

export const DEFAULT_MATRIX_WIDTH = 1440;
export const DEFAULT_MATRIX_HEIGHT = 620;

export interface MatrixLayout {
  width: number;
  height: number;
  left: number;
  top: number;
  cx: number;
  cy: number;
  rx: number;
  by: number;
}

export interface MatrixPoint {
  o: OpportunityCandidate;
  x: number;
  y: number;
  r: number;
}

export interface MatrixLabelPlacement {
  id: string;
  title: string;
  width: number;
  r: number;
  bubbleX: number;
  bubbleCy: number;
  bubbleTop: number;
  centerX: number;
  pillBottom: number;
  onBubble: boolean;
  onX: number;
  onY: number;
}

export interface MatrixGeometry {
  layout: MatrixLayout;
  points: MatrixPoint[];
  placements: MatrixLabelPlacement[];
}

/** Plot insets (in virtual px) around the chart box: the left gutter holds the
 *  impact axis labels, the bottom holds the effort labels. Optional — the
 *  on-screen chart uses the responsive defaults; the PDF passes tighter values
 *  to widen the plot box toward the page edges. */
export interface MatrixMargins {
  left: number;
  right: number;
  top: number;
  bottom: number;
}

export function createLayout(
  width: number,
  height: number,
  margins?: Partial<MatrixMargins>,
): MatrixLayout {
  const safeWidth = Math.max(width, 360);
  const safeHeight = Math.max(height, 360);
  const left = margins?.left ?? (safeWidth < 640 ? 92 : 150);
  const right = margins?.right ?? (safeWidth < 640 ? 18 : 36);
  const top = margins?.top ?? 28;
  const bottom = margins?.bottom ?? 38;
  const rx = safeWidth - right;
  const by = safeHeight - bottom;

  return {
    width: safeWidth,
    height: safeHeight,
    left,
    top,
    cx: left + (rx - left) / 2,
    cy: top + (by - top) / 2,
    rx,
    by,
  };
}

export function buildPoints(
  opportunities: OpportunityCandidate[],
  layout: MatrixLayout,
): MatrixPoint[] {
  const W = layout.rx - layout.left;
  const H = layout.by - layout.top;
  return opportunities.map((o) => ({
    o,
    x: layout.left + ((o.effort - 1) / 9) * W,
    y: layout.by - ((o.impact - 1) / 9) * H,
    r: clamp(10 + o.impact * 3, 12, 38),
  }));
}

/**
 * Place each bubble's name label. Non-overlapping bubbles get a pill just above
 * the bubble (with a leader line). Bubbles that physically overlap are clustered
 * (union-find) and labelled directly on the bubble, biased left/right so each
 * name clearly belongs to its circle.
 */
export function computeLabelPlacements(
  points: MatrixPoint[],
  layout: MatrixLayout,
): MatrixLabelPlacement[] {
  const LABEL_H = 21;

  const clampX = (cx: number, width: number) =>
    clamp(cx, layout.left + width / 2 + 2, layout.rx - width / 2 - 2);

  const placed: MatrixLabelPlacement[] = points.map((p) => {
    const title = p.o.title.length > 26 ? `${p.o.title.slice(0, 26)}...` : p.o.title;
    const width = clamp(title.length * 6.7 + 16, 90, 210);
    return {
      id: p.o.id,
      title,
      width,
      r: p.r,
      bubbleX: p.x,
      bubbleCy: p.y,
      bubbleTop: p.y - p.r,
      centerX: clampX(p.x, width),
      pillBottom: clamp(p.y - p.r - 4, layout.top + LABEL_H, layout.by - 2),
      onBubble: false,
      onX: p.x,
      onY: p.y,
    };
  });

  const parent = placed.map((_, i) => i);
  const find = (i: number): number => (parent[i] === i ? i : (parent[i] = find(parent[i])));
  for (let i = 0; i < placed.length; i += 1) {
    for (let j = i + 1; j < placed.length; j += 1) {
      const dist = Math.hypot(
        placed[i].bubbleX - placed[j].bubbleX,
        placed[i].bubbleCy - placed[j].bubbleCy,
      );
      if (dist < placed[i].r + placed[j].r) parent[find(i)] = find(j);
    }
  }
  const clusters = new Map<number, number[]>();
  placed.forEach((_, i) => {
    const root = find(i);
    if (!clusters.has(root)) clusters.set(root, []);
    clusters.get(root)!.push(i);
  });

  clusters.forEach((idxs) => {
    if (idxs.length < 2) return;
    const meanX = idxs.reduce((s, i) => s + placed[i].bubbleX, 0) / idxs.length;
    idxs.forEach((i) => {
      placed[i].onBubble = true;
      const extendRight = placed[i].bubbleX >= meanX;
      const rawX = extendRight
        ? placed[i].bubbleX + placed[i].width / 2
        : placed[i].bubbleX - placed[i].width / 2;
      placed[i].onX = clampX(rawX, placed[i].width);
      const yOffset = extendRight ? 0 : Math.max(22, placed[i].r * 0.9);
      placed[i].onY = clamp(placed[i].bubbleCy + yOffset, layout.top + 12, layout.by - 12);
    });
  });

  return placed;
}

export function computeMatrixGeometry(
  opportunities: OpportunityCandidate[],
  width: number,
  height: number,
  margins?: Partial<MatrixMargins>,
): MatrixGeometry {
  const layout = createLayout(width, height, margins);
  const points = buildPoints(opportunities, layout).sort((a, b) => b.r - a.r);
  const placements = computeLabelPlacements(points, layout);
  return { layout, points, placements };
}

// ── SVG serialization (used by the PDF export to embed a chart identical to the
//    on-screen one) ───────────────────────────────────────────────────────────

export interface MatrixPalette {
  grid: string;
  axisLabel: string;
  quadrantPrimary: string;
  quadrantMuted: string;
  labelWeight: string | number;
  labelStrong: string;
  labelMid: string;
  labelMuted: string;
  bubbleFill: string;
  bubbleStroke: string;
  bubbleLabelBg: string;
  bubbleLabelBorder: string;
  hoverLabel: string;
}

/** Concrete light-theme colors — mirrors :root.theme-light in styles.css, so a
 *  chart serialized with this palette looks like the on-screen chart in light
 *  mode. Legacy rgba() form for maximum SVG-rasterization compatibility. */
export const LIGHT_MATRIX_PALETTE: MatrixPalette = {
  grid: 'rgba(92,112,145,0.62)',
  axisLabel: 'rgba(64,78,105,0.86)',
  quadrantPrimary: 'rgba(13,85,215,0.08)',
  quadrantMuted: 'rgba(13,85,215,0.015)',
  labelWeight: 600,
  labelStrong: 'rgba(40,55,82,0.92)',
  labelMid: 'rgba(48,65,96,0.84)',
  labelMuted: 'rgba(64,78,105,0.78)',
  bubbleFill: 'rgba(62,92,138,0.24)',
  bubbleStroke: 'rgba(66,93,132,0.62)',
  bubbleLabelBg: 'rgba(255,255,255,0.95)',
  bubbleLabelBorder: 'rgba(13,85,215,0.30)',
  hoverLabel: 'rgba(7,25,58,0.86)',
};

function escapeXml(s: string): string {
  return s
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

/**
 * Serialize the Effort vs Impact matrix to a standalone SVG string. Mirrors the
 * markup that SnapshotMatrix renders on-screen (same geometry, same element
 * structure), but with an explicit palette + font so it rasterizes identically
 * outside the app. Used by the PDF export.
 */
export function buildMatrixSvg(
  opportunities: OpportunityCandidate[],
  width: number,
  height: number,
  palette: MatrixPalette,
  margins?: Partial<MatrixMargins>,
): string {
  const { layout, points, placements } = computeMatrixGeometry(opportunities, width, height, margins);
  const parts: string[] = [];

  parts.push(
    `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${layout.width} ${layout.height}" ` +
      `width="${layout.width}" height="${layout.height}" ` +
      `font-family="ui-sans-serif, system-ui, -apple-system, 'Segoe UI', Roboto, Arial, sans-serif">`,
  );
  parts.push(`<rect x="0" y="0" width="${layout.width}" height="${layout.height}" fill="#ffffff"/>`);

  // Quadrant tints.
  parts.push(`<rect x="${layout.left}" y="${layout.top}" width="${layout.cx - layout.left}" height="${layout.cy - layout.top}" fill="${palette.quadrantPrimary}"/>`);
  parts.push(`<rect x="${layout.cx}" y="${layout.top}" width="${layout.rx - layout.cx}" height="${layout.cy - layout.top}" fill="${palette.quadrantMuted}"/>`);
  parts.push(`<rect x="${layout.left}" y="${layout.cy}" width="${layout.cx - layout.left}" height="${layout.by - layout.cy}" fill="${palette.quadrantMuted}"/>`);
  parts.push(`<rect x="${layout.cx}" y="${layout.cy}" width="${layout.rx - layout.cx}" height="${layout.by - layout.cy}" fill="${palette.quadrantMuted}"/>`);

  // Plot border + center cross.
  parts.push(`<rect x="${layout.left}" y="${layout.top}" width="${layout.rx - layout.left}" height="${layout.by - layout.top}" fill="none" stroke="${palette.grid}" stroke-width="1"/>`);
  parts.push(`<line x1="${layout.cx}" y1="${layout.top}" x2="${layout.cx}" y2="${layout.by}" stroke="${palette.grid}" stroke-width="1"/>`);
  parts.push(`<line x1="${layout.left}" y1="${layout.cy}" x2="${layout.rx}" y2="${layout.cy}" stroke="${palette.grid}" stroke-width="1"/>`);

  // Axis labels.
  parts.push(`<text x="${layout.left - 10}" y="${layout.top + 18}" font-size="16" font-weight="600" fill="${palette.axisLabel}" text-anchor="end">HIGH IMPACT</text>`);
  parts.push(`<text x="${layout.left - 10}" y="${layout.by - 6}" font-size="16" font-weight="600" fill="${palette.axisLabel}" text-anchor="end">LOW IMPACT</text>`);
  parts.push(`<text x="${layout.left}" y="${layout.height - 10}" font-size="16" font-weight="600" fill="${palette.axisLabel}">LOW EFFORT</text>`);
  parts.push(`<text x="${layout.rx}" y="${layout.height - 10}" font-size="16" font-weight="600" fill="${palette.axisLabel}" text-anchor="end">HIGH EFFORT</text>`);

  // Bubbles.
  points.forEach((p) => {
    parts.push(`<circle cx="${p.x}" cy="${p.y}" r="${p.r}" fill="${palette.bubbleFill}" stroke="${palette.bubbleStroke}" stroke-width="1.5"/>`);
  });

  // Quadrant labels.
  const quadLabels: Array<[number, number, string, string]> = [
    [layout.left + 14, layout.top + 24, 'QUICK WINS', palette.labelStrong],
    [layout.cx + 14, layout.top + 24, 'HIGH VALUE', palette.labelMid],
    [layout.left + 14, layout.cy + 24, 'FOUNDATION', palette.labelMuted],
    [layout.cx + 14, layout.cy + 24, 'LONG TERM', palette.labelMuted],
  ];
  quadLabels.forEach(([x, ly, label, fill]) => {
    parts.push(`<text x="${x}" y="${ly - 1}" font-size="14" font-weight="${palette.labelWeight}" letter-spacing="0.4" fill="${fill}">${label}</text>`);
  });

  // Non-overlapping labels: leader line + pill above the bubble.
  placements
    .filter((lab) => !lab.onBubble)
    .forEach((lab) => {
      const originX = clamp(lab.bubbleX, lab.centerX - lab.width / 2, lab.centerX + lab.width / 2);
      parts.push(`<line x1="${originX}" y1="${lab.pillBottom}" x2="${lab.bubbleX}" y2="${lab.bubbleTop}" stroke="${palette.bubbleLabelBorder}" stroke-width="1"/>`);
      parts.push(`<rect x="${lab.centerX - lab.width / 2}" y="${lab.pillBottom - 21}" width="${lab.width}" height="21" rx="6" fill="${palette.bubbleLabelBg}" stroke="${palette.bubbleLabelBorder}"/>`);
      parts.push(`<text x="${lab.centerX}" y="${lab.pillBottom - 6}" font-size="13" font-weight="600" fill="${palette.hoverLabel}" text-anchor="middle">${escapeXml(lab.title)}</text>`);
    });

  // Overlapping labels: pill drawn on the bubble.
  placements
    .filter((lab) => lab.onBubble)
    .forEach((lab) => {
      parts.push(`<rect x="${lab.onX - lab.width / 2}" y="${lab.onY - 10}" width="${lab.width}" height="20" rx="5" fill="${palette.bubbleLabelBg}" stroke="${palette.bubbleLabelBorder}"/>`);
      parts.push(`<text x="${lab.onX}" y="${lab.onY + 4}" font-size="12" font-weight="600" fill="${palette.hoverLabel}" text-anchor="middle">${escapeXml(lab.title)}</text>`);
    });

  parts.push('</svg>');
  return parts.join('');
}
