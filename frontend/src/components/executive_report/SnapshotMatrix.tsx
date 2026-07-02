import React, { useEffect, useMemo, useRef, useState } from 'react';
import { OpportunityCandidate } from '../../types/analystReview';
import { resolveLabelOverlaps } from '../../utils/matrixLayout';

function clamp(n: number, a: number, b: number) {
  return Math.max(a, Math.min(b, n));
}

const DEFAULT_WIDTH = 1440;
const DEFAULT_HEIGHT = 620;

type MatrixLayout = {
  width: number;
  height: number;
  left: number;
  top: number;
  cx: number;
  cy: number;
  rx: number;
  by: number;
};

function createLayout(width: number, height: number): MatrixLayout {
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

function buildPoints(opportunities: OpportunityCandidate[], layout: MatrixLayout) {
  const W = layout.rx - layout.left;
  const H = layout.by - layout.top;
  return opportunities.map((o) => ({
    o,
    x: layout.left + ((o.effort - 1) / 9) * W,
    y: layout.by - ((o.impact - 1) / 9) * H,
    r: clamp(10 + o.impact * 3, 12, 38),
  }));
}

export interface OverlapBubble {
  bubbleX: number;
  bubbleCy: number;
  r: number;
}

/**
 * Cluster bubbles that physically overlap (centre distance < sum of radii)
 * using union-find, so a chain of overlapping bubbles (A–B and B–C, but not
 * A–C directly) is still treated as a single group. Returns one entry per
 * cluster, each an array of input indices; every input bubble appears in
 * exactly one cluster (singletons included), so the result is a partition of
 * the input indices.
 *
 * Extracted from the label-placement memo as a pure, side-effect-free helper so
 * the overlap/transitivity logic can be unit-tested without rendering the SVG.
 */
export function clusterOverlappingBubbles(bubbles: OverlapBubble[]): number[][] {
  const parent = bubbles.map((_, i) => i);
  const find = (i: number): number =>
    parent[i] === i ? i : (parent[i] = find(parent[i]));
  for (let i = 0; i < bubbles.length; i += 1) {
    for (let j = i + 1; j < bubbles.length; j += 1) {
      const dist = Math.hypot(
        bubbles[i].bubbleX - bubbles[j].bubbleX,
        bubbles[i].bubbleCy - bubbles[j].bubbleCy,
      );
      if (dist < bubbles[i].r + bubbles[j].r) parent[find(i)] = find(j);
    }
  }
  const clusters = new Map<number, number[]>();
  bubbles.forEach((_, i) => {
    const root = find(i);
    if (!clusters.has(root)) clusters.set(root, []);
    clusters.get(root)!.push(i);
  });
  return Array.from(clusters.values());
}

interface SnapshotMatrixProps {
  opportunities: OpportunityCandidate[];
}

export default function SnapshotMatrix({ opportunities }: SnapshotMatrixProps) {
  const plotRef = useRef<HTMLDivElement>(null);
  const [viewport, setViewport] = useState({
    width: DEFAULT_WIDTH,
    height: DEFAULT_HEIGHT,
  });
  const layout = useMemo(
    () => createLayout(viewport.width, viewport.height),
    [viewport.height, viewport.width],
  );
  const points = useMemo(
    () => buildPoints(opportunities, layout).sort((a, b) => b.r - a.r),
    [opportunities, layout],
  );

  // Name labels. Non-overlapping bubbles keep their label in a pill just above the
  // bubble (with a short leader line). Bubbles that physically overlap another bubble
  // are flagged `onBubble` — their name is written directly on the bubble instead, so
  // it's obvious which name belongs to which of the overlapping circles.
  const labelPlacements = useMemo(() => {
    const LABEL_H = 21;

    const clampX = (cx: number, width: number) =>
      clamp(cx, layout.left + width / 2 + 2, layout.rx - width / 2 - 2);

    const placed = points.map((p) => {
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
        onX: p.x,   // on-bubble label centre x (set for overlapping clusters)
        onY: p.y,   // on-bubble label centre y
      };
    });

    // Cluster bubbles that physically overlap (centre distance < sum of radii)
    // so a chain of overlapping bubbles is treated as one group. The union-find
    // clustering is a pure, unit-tested helper (clusterOverlappingBubbles).
    const clusters = clusterOverlappingBubbles(placed);

    // For each overlapping cluster, draw each name as a bar that sticks OUT of its
    // bubble: a bubble on the right side of the cluster extends its label to the
    // right, a bubble on the left extends to the left — the label's inner edge is
    // anchored at the bubble centre. Vertically each label sits at its own bubble's
    // centre, so an upper bubble's name rides higher and a lower bubble's rides lower
    // (matches the requested tag layout).
    clusters.forEach((idxs) => {
      if (idxs.length < 2) return;
      const meanX = idxs.reduce((s, i) => s + placed[i].bubbleX, 0) / idxs.length;
      idxs.forEach((i) => {
        placed[i].onBubble = true;
        const extendRight = placed[i].bubbleX >= meanX;
        // Inner edge at the bubble centre; the bar then extends outward.
        const rawX = extendRight
          ? placed[i].bubbleX + placed[i].width / 2
          : placed[i].bubbleX - placed[i].width / 2;
        placed[i].onX = clampX(rawX, placed[i].width);
        // Drop the left-extending (lower) label to the bottom of its bubble so it
        // sits clearly below the upper bubble instead of crossing into it.
        const yOffset = extendRight ? 0 : Math.max(22, placed[i].r * 0.9);
        placed[i].onY = clamp(placed[i].bubbleCy + yOffset, layout.top + 12, layout.by - 12);
      });
    });

    // Nudge any label boxes that still overlap apart vertically (close-but-not-
    // overlapping bubbles' pills, or an on-bubble label near a pill). Shared with
    // the PDF export so the downloaded chart matches the on-screen one.
    return resolveLabelOverlaps(placed, layout);
  }, [points, layout]);

  useEffect(() => {
    const node = plotRef.current;
    if (!node) return;

    const updateViewport = () => {
      const rect = node.getBoundingClientRect();
      if (!rect.width || !rect.height) return;
      const next = {
        width: Math.round(rect.width),
        height: Math.round(rect.height),
      };
      setViewport((current) =>
        current.width === next.width && current.height === next.height
          ? current
          : next,
      );
    };

    updateViewport();
    if (typeof ResizeObserver === 'undefined') return;

    const observer = new ResizeObserver(updateViewport);
    observer.observe(node);
    return () => observer.disconnect();
  }, []);

  return (
    <div className="flex h-[560px] min-h-0 flex-col rounded-xl border border-border bg-panel p-4 lg:h-[720px]">
      <div className="mb-3 flex shrink-0 flex-wrap items-center justify-between gap-2">
        <div className="pb-2 text-xl font-semibold text-text">Effort vs Impact</div>
        <div className="text-xs text-muted">Read-only opportunity snapshot</div>
      </div>

      <div ref={plotRef} className="aspect-[1440/620] min-h-[320px] flex-1 overflow-hidden rounded-lg border border-border bg-bg/10 lg:aspect-auto lg:min-h-0">
        <svg
          viewBox={`0 0 ${layout.width} ${layout.height}`}
          width="100%"
          height="100%"
          preserveAspectRatio="xMidYMid meet"
          style={{ display: 'block' }}
        >
          <rect x={layout.left} y={layout.top} width={layout.cx - layout.left} height={layout.cy - layout.top} fill="var(--opportunity-matrix-quadrant-primary)" />
          <rect x={layout.cx} y={layout.top} width={layout.rx - layout.cx} height={layout.cy - layout.top} fill="var(--opportunity-matrix-quadrant-muted)" />
          <rect x={layout.left} y={layout.cy} width={layout.cx - layout.left} height={layout.by - layout.cy} fill="var(--opportunity-matrix-quadrant-muted)" />
          <rect x={layout.cx} y={layout.cy} width={layout.rx - layout.cx} height={layout.by - layout.cy} fill="var(--opportunity-matrix-quadrant-muted)" />

          <rect
            x={layout.left}
            y={layout.top}
            width={layout.rx - layout.left}
            height={layout.by - layout.top}
            fill="none"
            stroke="var(--opportunity-matrix-grid)"
            strokeWidth="1"
          />

          <line x1={layout.cx} y1={layout.top} x2={layout.cx} y2={layout.by} stroke="var(--opportunity-matrix-grid)" strokeWidth="1" />
          <line x1={layout.left} y1={layout.cy} x2={layout.rx} y2={layout.cy} stroke="var(--opportunity-matrix-grid)" strokeWidth="1" />

          <text x={layout.left - 10} y={layout.top + 18} fontSize="16" fontWeight="600" fill="var(--opportunity-matrix-axis-label)" textAnchor="end">
            HIGH IMPACT
          </text>
          <text x={layout.left - 10} y={layout.by - 6} fontSize="16" fontWeight="600" fill="var(--opportunity-matrix-axis-label)" textAnchor="end">
            LOW IMPACT
          </text>

          <text x={layout.left} y={layout.height - 10} fontSize="16" fontWeight="600" fill="var(--opportunity-matrix-axis-label)">
            LOW EFFORT
          </text>
          <text x={layout.rx} y={layout.height - 10} fontSize="16" fontWeight="600" fill="var(--opportunity-matrix-axis-label)" textAnchor="end">
            HIGH EFFORT
          </text>

          {points.map(({ o, x, y, r }) => (
            <circle
              key={o.id}
              cx={x}
              cy={y}
              r={r}
              fill="var(--opportunity-matrix-bubble-fill)"
              stroke="var(--opportunity-matrix-bubble-stroke)"
              strokeWidth="1.5"
            >
              <title>{o.title}</title>
            </circle>
          ))}

          {[
            { x: layout.left + 14, y: layout.top + 24, label: 'QUICK WINS', fill: 'var(--opportunity-matrix-label-strong)' },
            { x: layout.cx + 14, y: layout.top + 24, label: 'HIGH VALUE', fill: 'var(--opportunity-matrix-label-mid)' },
            { x: layout.left + 14, y: layout.cy + 24, label: 'FOUNDATION', fill: 'var(--opportunity-matrix-label-muted)' },
            { x: layout.cx + 14, y: layout.cy + 24, label: 'LONG TERM', fill: 'var(--opportunity-matrix-label-muted)' },
          ].map(({ x, y, label, fill }) => (
            <g key={label} pointerEvents="none">
              <text x={x} y={y - 1} fontSize="14" fontWeight="var(--opportunity-matrix-label-weight)" letterSpacing="0.4" fill={fill}>
                {label}
              </text>
            </g>
          ))}

          {/* NON-OVERLAPPING bubbles: leader line from the pill down to the bubble. */}
          {labelPlacements.filter((lab) => !lab.onBubble).map((lab) => {
            const originX = clamp(lab.bubbleX, lab.centerX - lab.width / 2, lab.centerX + lab.width / 2);
            return (
              <line
                key={`leader-${lab.id}`}
                x1={originX}
                y1={lab.pillBottom}
                x2={lab.bubbleX}
                y2={lab.bubbleTop}
                stroke="var(--opportunity-matrix-bubble-label-border)"
                strokeWidth="1"
                pointerEvents="none"
              />
            );
          })}

          {/* NON-OVERLAPPING bubbles: name in a pill just above the bubble. */}
          {labelPlacements.filter((lab) => !lab.onBubble).map((lab) => (
            <g key={`label-${lab.id}`} pointerEvents="none">
              <rect
                x={lab.centerX - lab.width / 2}
                y={lab.pillBottom - 21}
                width={lab.width}
                height={21}
                rx={6}
                fill="var(--opportunity-matrix-bubble-label-bg)"
                stroke="var(--opportunity-matrix-bubble-label-border)"
              />
              <text
                x={lab.centerX}
                y={lab.pillBottom - 6}
                fontSize="13"
                fontWeight="600"
                fill="var(--opportunity-matrix-hover-label)"
                textAnchor="middle"
              >
                {lab.title}
              </text>
            </g>
          ))}

          {/* OVERLAPPING bubbles: names written ON the bubbles, side by side at the
              same height (left bubble's name biased left, right biased right). Drawn
              last so they sit above every bubble fill. */}
          {labelPlacements.filter((lab) => lab.onBubble).map((lab) => (
            <g key={`on-${lab.id}`} pointerEvents="none">
              <rect
                x={lab.onX - lab.width / 2}
                y={lab.onY - 10}
                width={lab.width}
                height={20}
                rx={5}
                fill="var(--opportunity-matrix-bubble-label-bg)"
                stroke="var(--opportunity-matrix-bubble-label-border)"
              />
              <text
                x={lab.onX}
                y={lab.onY + 4}
                fontSize="12"
                fontWeight="600"
                fill="var(--opportunity-matrix-hover-label)"
                textAnchor="middle"
              >
                {lab.title}
              </text>
            </g>
          ))}
        </svg>
      </div>
    </div>
  );
}
