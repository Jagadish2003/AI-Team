/**
 * matrixLayout — geometry + SVG serialization used by the on-screen chart and
 * the PDF export. Verifies buildMatrixSvg (the chart the PDF rasterizes) emits
 * valid SVG covering the bubbles, axes, quadrants, and labels.
 *
 * Run:
 *   npx vitest run src/__tests__/matrixLayout.test.ts
 */
import { describe, it, expect } from 'vitest';
import {
  buildMatrixSvg,
  computeMatrixGeometry,
  createLayout,
  resolveLabelOverlaps,
  LIGHT_MATRIX_PALETTE,
} from '../utils/matrixLayout';
import type { MatrixLabelPlacement } from '../utils/matrixLayout';
import type { OpportunityCandidate } from '../types/analystReview';

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

const OPPS = [
  opp({ id: 'a', title: 'Checklist Bottleneck', impact: 7, effort: 2 }),
  opp({ id: 'b', title: 'Covenant Tracking Gap', impact: 8, effort: 6 }),
  opp({ id: 'c', title: 'Spreading Bottleneck', impact: 8, effort: 5 }),
];

describe('matrixLayout', () => {
  it('computes one point per opportunity with clamped radii', () => {
    const { points } = computeMatrixGeometry(OPPS, 1440, 720);
    expect(points).toHaveLength(3);
    points.forEach((p) => {
      expect(p.r).toBeGreaterThanOrEqual(12);
      expect(p.r).toBeLessThanOrEqual(38);
    });
  });

  it('buildMatrixSvg emits valid SVG with a bubble per opportunity', () => {
    const svg = buildMatrixSvg(OPPS, 1440, 720, LIGHT_MATRIX_PALETTE);

    expect(svg.startsWith('<svg')).toBe(true);
    expect(svg).toContain('viewBox="0 0 1440 720"');
    expect(svg.trimEnd().endsWith('</svg>')).toBe(true);

    // One <circle> bubble per opportunity.
    expect(svg.match(/<circle /g)).toHaveLength(3);

    // Axes + quadrant labels present.
    expect(svg).toContain('HIGH IMPACT');
    expect(svg).toContain('LOW EFFORT');
    expect(svg).toContain('QUICK WINS');

    // Bubble names rendered (and XML-escaped).
    expect(svg).toContain('Checklist Bottleneck');
    expect(svg).toContain('Covenant Tracking Gap');

    // Light palette colors used, not raw CSS variables.
    expect(svg).not.toContain('var(--');
    expect(svg).toContain('rgba(');
  });

  it('escapes XML-significant characters in titles', () => {
    const svg = buildMatrixSvg([opp({ id: 'z', title: 'A & B <C>' })], 1440, 720, LIGHT_MATRIX_PALETTE);
    expect(svg).toContain('A &amp; B &lt;C&gt;');
    expect(svg).not.toMatch(/A & B <C>/);
  });

  it('does not emit overlapping name labels for close-but-not-overlapping bubbles', () => {
    // Two Quick Wins near each other: distinct bubbles, but their pill labels
    // would collide without de-collision.
    const geom = computeMatrixGeometry(
      [
        opp({ id: 'p1', title: 'Automate repetitive Case processing', impact: 8, effort: 2 }),
        opp({ id: 'p2', title: 'Automate knowledge article suggestions', impact: 8, effort: 2 }),
      ],
      1440,
      720,
    );
    const boxes = geom.placements.map((p) => box(p));
    expect(anyOverlap(boxes)).toBe(false);
  });
});

// ── resolveLabelOverlaps — label de-collision ────────────────────────────────
type Box = { l: number; r: number; t: number; b: number };

function box(p: MatrixLabelPlacement): Box {
  const h = p.onBubble ? 20 : 21;
  const cx = p.onBubble ? p.onX : p.centerX;
  const cy = p.onBubble ? p.onY : p.pillBottom - h / 2;
  return { l: cx - p.width / 2, r: cx + p.width / 2, t: cy - h / 2, b: cy + h / 2 };
}

function overlaps(a: Box, b: Box): boolean {
  return a.l < b.r && b.l < a.r && a.t < b.b && b.t < a.b;
}

function anyOverlap(boxes: Box[]): boolean {
  for (let i = 0; i < boxes.length; i += 1) {
    for (let j = i + 1; j < boxes.length; j += 1) {
      if (overlaps(boxes[i], boxes[j])) return true;
    }
  }
  return false;
}

describe('resolveLabelOverlaps', () => {
  const layout = createLayout(1440, 720);

  const place = (over: Partial<MatrixLabelPlacement>): MatrixLabelPlacement => ({
    id: 'x',
    title: 'Label',
    width: 200,
    r: 20,
    bubbleX: 300,
    bubbleCy: 300,
    bubbleTop: 280,
    centerX: 300,
    pillBottom: 270,
    onBubble: false,
    onX: 300,
    onY: 300,
    ...over,
  });

  it('separates two colliding pill labels', () => {
    const placed = [
      place({ id: 'a', centerX: 300, pillBottom: 270 }),
      place({ id: 'b', centerX: 320, pillBottom: 272 }),
    ];
    resolveLabelOverlaps(placed, layout);
    expect(anyOverlap(placed.map(box))).toBe(false);
  });

  it('leaves already-separated labels essentially in place', () => {
    const placed = [
      place({ id: 'a', centerX: 200, pillBottom: 200 }),
      place({ id: 'b', centerX: 900, pillBottom: 500 }),
    ];
    const before = placed.map((p) => p.pillBottom);
    resolveLabelOverlaps(placed, layout);
    // Far apart horizontally → untouched.
    expect(placed.map((p) => p.pillBottom)).toEqual(before);
  });

  it('keeps every label box within the plot bounds', () => {
    const placed = Array.from({ length: 6 }, (_, i) =>
      place({ id: `n${i}`, centerX: 300, pillBottom: 300 }),
    );
    resolveLabelOverlaps(placed, layout);
    placed.map(box).forEach((bx) => {
      expect(bx.t).toBeGreaterThanOrEqual(layout.top);
      expect(bx.b).toBeLessThanOrEqual(layout.by);
    });
  });
});
