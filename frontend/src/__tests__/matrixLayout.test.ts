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
  LIGHT_MATRIX_PALETTE,
} from '../utils/matrixLayout';
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
});
