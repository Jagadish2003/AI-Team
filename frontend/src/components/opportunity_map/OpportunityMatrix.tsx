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

function buildPoints(filtered: OpportunityCandidate[], layout: MatrixLayout) {
  const W = layout.rx - layout.left;
  const H = layout.by - layout.top;
  return filtered.map((o) => {
    const x = layout.left + ((o.effort - 1) / 9) * W;
    const y = layout.by - ((o.impact - 1) / 9) * H;
    const r = clamp(10 + o.impact * 3, 12, 38);
    return { o, x, y, r };
  });
}

function bubbleStyle(opportunity: OpportunityCandidate, isSelected: boolean, isHover: boolean) {
  if (opportunity.decision === 'APPROVED') {
    return { fill: 'rgba(0,180,120,0.35)', stroke: '#00b478' };
  }

  if (opportunity.decision === 'REJECTED') {
    return { fill: 'rgba(180,60,60,0.35)', stroke: '#b43c3c' };
  }

  if (isSelected) {
    return { fill: 'rgba(13,85,215,0.28)', stroke: '#0D55D7' };
  }

  if (isHover) {
    return {
      fill: 'var(--opportunity-matrix-hover-fill)',
      stroke: 'var(--opportunity-matrix-hover-stroke)',
    };
  }

  return {
    fill: 'var(--opportunity-matrix-bubble-fill)',
    stroke: 'var(--opportunity-matrix-bubble-stroke)',
  };
}

export default function OpportunityMatrix({
  filtered,
  selectedId,
  onSelect,
}: {
  filtered: OpportunityCandidate[];
  selectedId: string | null;
  onSelect: (id: string) => void;
}) {
  const [hoverId, setHoverId] = useState<string | null>(null);
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
    () => buildPoints(filtered, layout),
    [filtered, layout],
  );
  const impacts = new Set(filtered.map((o) => o.impact));
  const efforts = new Set(filtered.map((o) => o.effort));
  const scoresCollapsed = filtered.length > 1 && (impacts.size === 1 || efforts.size === 1);

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
    <div className="flex h-full min-h-0 flex-col rounded-xl border border-border bg-panel p-4">
      <div className="mb-3 flex shrink-0 flex-wrap items-center justify-between gap-2">
        <div className="pb-2 text-xl font-semibold text-text">Effort vs Impact</div>
        <div className="text-xs text-muted">Click a bubble to preview details</div>
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

          {[...points].sort((a, b) => b.r - a.r).map((p) => {
            const isSelected = p.o.id === selectedId;
            const isHover = p.o.id === hoverId;
            const { fill, stroke } = bubbleStyle(p.o, isSelected, isHover);

            return (
              <g
                key={p.o.id}
                role="button"
                aria-label={`Select opportunity: ${p.o.title}`}
                style={{ cursor: 'pointer' }}
                onMouseEnter={() => setHoverId(p.o.id)}
                onMouseLeave={() => setHoverId(null)}
                onClick={() => onSelect(p.o.id)}
              >
                <circle
                  cx={p.x}
                  cy={p.y}
                  r={p.r}
                  fill={fill}
                  stroke={stroke}
                  strokeWidth={isSelected ? 2.5 : 1.5}
                  style={{ transition: 'fill 0.15s, stroke 0.15s' }}
                />
              </g>
            );
          })}

          {[
            { x: layout.left + 14, y: layout.top + 24, label: 'QUICK WINS', fill: 'var(--opportunity-matrix-label-strong)', w: 102 },
            { x: layout.cx + 14, y: layout.top + 24, label: 'HIGH VALUE', fill: 'var(--opportunity-matrix-label-mid)', w: 98 },
            { x: layout.left + 14, y: layout.cy + 24, label: 'FOUNDATION', fill: 'var(--opportunity-matrix-label-muted)', w: 120 },
            { x: layout.cx + 14, y: layout.cy + 24, label: 'LONG TERM', fill: 'var(--opportunity-matrix-label-muted)', w: 94 },
          ].map(({ x, y, label, fill }) => (
            <g key={label} pointerEvents="none">
              <text x={x} y={y - 1} fontSize="14" fontWeight="var(--opportunity-matrix-label-weight)" letterSpacing="0.4" fill={fill}>
                {label}
              </text>
            </g>
          ))}

          {points.map((p) => {
            const isSelected = p.o.id === selectedId;
            const isHover = p.o.id === hoverId;
            if (!isHover && !isSelected) return null;
            const title = p.o.title.length > 34 ? `${p.o.title.slice(0, 34)}...` : p.o.title;
            const labelX = clamp(p.x, layout.left + 92, layout.rx - 92);
            const labelY = clamp(p.y - p.r - 15, layout.top + 26, layout.by - 12);
            const labelWidth = clamp(title.length * 7.1 + 18, 96, 270);
            return (
              <g key={`label-${p.o.id}`} pointerEvents="none">
                <rect
                  x={labelX - labelWidth / 2}
                  y={labelY - 17}
                  width={labelWidth}
                  height={23}
                  rx={6}
                  fill="var(--opportunity-matrix-bubble-label-bg)"
                  stroke="var(--opportunity-matrix-bubble-label-border)"
                />
                <text
                  x={labelX}
                  y={labelY}
                  fontSize="13"
                  fontWeight="600"
                  fill={isSelected ? 'var(--opportunity-matrix-selected-label)' : 'var(--opportunity-matrix-hover-label)'}
                  textAnchor="middle"
                >
                  {title}
                </text>
              </g>
            );
          })}
        </svg>
      </div>

      {scoresCollapsed ? (
        <div
          className="mt-3 shrink-0 rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs text-amber-300"
          data-testid="score-collapse-warning"
        >
          All opportunities have identical scores - the quadrant cannot show meaningful spread.
          Apply T41-6 scorer recalibration to produce distinct impact and effort values.
        </div>
      ) : (
        <div className="mt-3 shrink-0 text-xs text-muted">
          Approve or reject an opportunity in the detail panel - the bubble color updates in real time.
        </div>
      )}
    </div>
  );
}
