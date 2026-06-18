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

export function createLayout(width: number, height: number): MatrixLayout {
  const safeWidth = Math.max(width, 360);
  const safeHeight = Math.max(height, 360);
  const left = safeWidth < 640 ? 92 : 150;
  const right = safeWidth < 640 ? 18 : 36;
  const top = 28;
  const bottom = 38;
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
): MatrixGeometry {
  const layout = createLayout(width, height);
  const points = buildPoints(opportunities, layout).sort((a, b) => b.r - a.r);
  const placements = computeLabelPlacements(points, layout);
  return { layout, points, placements };
}
