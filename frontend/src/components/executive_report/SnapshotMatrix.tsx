import React, { useEffect, useMemo, useRef, useState } from 'react';
import { OpportunityCandidate } from '../../types/analystReview';

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

  // Static name labels sit above each bubble. When labels collide, two things keep
  // them readable: (1) they fan out horizontally by their bubble's x-position — the
  // left bubble's name shifts left, the right bubble's name shifts right; (2) they
  // stack vertically with a small gap. A thin leader line ties each pill to its
  // bubble so overlapping bubbles (e.g. Refund vs Discount approval) stay unambiguous.
  const labelPlacements = useMemo(() => {
    const LABEL_H = 21;
    const V_GAP = 3;       // vertical clearance when stacking
    const H_MARGIN = 6;    // horizontal slack before two labels count as overlapping
    const FAN_STEP = 70;   // horizontal offset between fanned labels in a cluster

    const clampX = (cx: number, width: number) =>
      clamp(cx, layout.left + width / 2 + 2, layout.rx - width / 2 - 2);

    const placed = points.map((p) => {
      const title = p.o.title.length > 26 ? `${p.o.title.slice(0, 26)}...` : p.o.title;
      const width = clamp(title.length * 6.7 + 16, 90, 210);
      return {
        id: p.o.id,
        title,
        width,
        bubbleX: p.x,
        bubbleTop: p.y - p.r,
        centerX: clampX(p.x, width),
        pillBottom: clamp(p.y - p.r - 4, layout.top + LABEL_H, layout.by - 2),
      };
    });

    const horizontallyOverlap = (a: typeof placed[number], b: typeof placed[number]) =>
      Math.abs(a.centerX - b.centerX) < (a.width + b.width) / 2 + H_MARGIN;
    const verticallyOverlap = (a: typeof placed[number], b: typeof placed[number]) =>
      Math.abs(a.pillBottom - b.pillBottom) < LABEL_H + V_GAP;

    // 1. Cluster labels that currently collide (overlap both horizontally and
    //    vertically), via simple union-find over the colliding pairs.
    const parent = placed.map((_, i) => i);
    const find = (i: number): number => (parent[i] === i ? i : (parent[i] = find(parent[i])));
    for (let i = 0; i < placed.length; i += 1) {
      for (let j = i + 1; j < placed.length; j += 1) {
        if (horizontallyOverlap(placed[i], placed[j]) && verticallyOverlap(placed[i], placed[j])) {
          parent[find(i)] = find(j);
        }
      }
    }
    const clusters = new Map<number, number[]>();
    placed.forEach((_, i) => {
      const root = find(i);
      if (!clusters.has(root)) clusters.set(root, []);
      clusters.get(root)!.push(i);
    });

    // 2. Within each colliding cluster, lay the labels out SIDE BY SIDE in a single
    //    row at the same height, ordered by bubble x — leftmost bubble gets the
    //    leftmost label, rightmost gets the rightmost. This keeps each name right at
    //    its bubble instead of stacking them one above another.
    const H_BETWEEN = 8; // horizontal gap between side-by-side labels
    clusters.forEach((idxs) => {
      if (idxs.length < 2) return;
      const ordered = idxs.slice().sort((a, b) => placed[a].bubbleX - placed[b].bubbleX);
      const meanX = ordered.reduce((s, i) => s + placed[i].bubbleX, 0) / ordered.length;
      const rowBottom = Math.min(...ordered.map((i) => placed[i].pillBottom));
      const totalWidth =
        ordered.reduce((s, i) => s + placed[i].width, 0) + H_BETWEEN * (ordered.length - 1);
      const available = layout.rx - layout.left - 4;

      if (totalWidth <= available) {
        // Fits on one row: place them left-to-right, centered on the cluster.
        let cursor = clamp(meanX - totalWidth / 2, layout.left + 2, layout.rx - 2 - totalWidth);
        ordered.forEach((i) => {
          placed[i].centerX = cursor + placed[i].width / 2;
          placed[i].pillBottom = rowBottom;
          cursor += placed[i].width + H_BETWEEN;
        });
      } else {
        // Too many wide labels to fit side by side — fall back to a fanned stack.
        ordered.forEach((i, k) => {
          const offset = (k - (ordered.length - 1) / 2) * FAN_STEP;
          placed[i].centerX = clampX(meanX + offset, placed[i].width);
          placed[i].pillBottom = clamp(rowBottom + k * (LABEL_H + V_GAP), layout.top + LABEL_H, layout.by - 2);
        });
      }
    });

    return placed;
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

          {/* Thin leader lines tying each label to its bubble (drawn under the pills).
              The line starts at the pill's bottom edge nearest the bubble so the
              connection reads cleanly even when a wide label sits off to one side. */}
          {labelPlacements.map((lab) => {
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

          {/* Static name labels — fanned horizontally by bubble position, stacked vertically */}
          {labelPlacements.map((lab) => (
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
        </svg>
      </div>
    </div>
  );
}
