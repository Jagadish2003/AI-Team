import React, { useEffect, useMemo, useRef, useState } from 'react';
import { OpportunityCandidate } from '../../types/analystReview';
import {
  clamp,
  computeMatrixGeometry,
  DEFAULT_MATRIX_HEIGHT,
  DEFAULT_MATRIX_WIDTH,
} from '../../utils/matrixLayout';

interface SnapshotMatrixProps {
  opportunities: OpportunityCandidate[];
}

export default function SnapshotMatrix({ opportunities }: SnapshotMatrixProps) {
  const plotRef = useRef<HTMLDivElement>(null);
  const [viewport, setViewport] = useState({
    width: DEFAULT_MATRIX_WIDTH,
    height: DEFAULT_MATRIX_HEIGHT,
  });

  const { layout, points, placements: labelPlacements } = useMemo(
    () => computeMatrixGeometry(opportunities, viewport.width, viewport.height),
    [opportunities, viewport.height, viewport.width],
  );

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
