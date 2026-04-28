import React, { useState } from 'react';
import { OpportunityCandidate } from '../../types/analystReview';

function clamp(n: number, a: number, b: number) { return Math.max(a, Math.min(b, n)); }

// SVG canvas constants — LEFT is wide enough for horizontal Y-axis labels
const VW = 860;
const VH = 620;
const LEFT  = 110;
const RIGHT = 20;
const TOP   = 20;
const BOT   = 30;

const CX = LEFT + (VW - LEFT - RIGHT) / 2;
const CY = TOP  + (VH - TOP  - BOT)  / 2;
const RX = VW - RIGHT;
const BY = VH - BOT;

function buildPoints(filtered: OpportunityCandidate[]) {
  const W = RX - LEFT;
  const H = BY - TOP;
  return filtered.map(o => {
    const x = LEFT + ((o.effort - 1) / 9) * W;
    const y = BY   - ((o.impact - 1) / 9) * H;
    const r = clamp(10 + o.impact * 3, 12, 38);
    return { o, x, y, r };
  });
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
  const points = buildPoints(filtered);

  return (
    <div className="rounded-xl border border-border bg-panel p-4">
      <div className="flex items-center justify-between mb-3">
        <div className="text-xl font-semibold text-text pb-2">Effort vs Impact</div>
        <div className="text-xs text-muted">Click a bubble to preview details</div>
      </div>

      <div className="rounded-lg border border-border bg-bg/10 overflow-hidden">
        <svg viewBox={`0 0 ${VW} ${VH}`} width="100%" style={{ display: 'block' }}>

          {/* Quadrant background fills */}
          <rect x={LEFT} y={TOP}  width={CX - LEFT} height={CY - TOP}  fill="rgba(0,180,180,0.03)" />
          <rect x={CX}   y={TOP}  width={RX - CX}   height={CY - TOP}  fill="rgba(255,255,255,0.01)" />
          <rect x={LEFT} y={CY}   width={CX - LEFT} height={BY - CY}   fill="rgba(255,255,255,0.01)" />
          <rect x={CX}   y={CY}   width={RX - CX}   height={BY - CY}   fill="rgba(255,255,255,0.01)" />

          {/* Outer border */}
          <rect x={LEFT} y={TOP} width={RX - LEFT} height={BY - TOP}
            fill="none" stroke="rgba(255,255,255,0.15)" strokeWidth="1" />

          {/* Quadrant dividers */}
          <line x1={CX} y1={TOP} x2={CX} y2={BY} stroke="rgba(255,255,255,0.15)" strokeWidth="1" />
          <line x1={LEFT} y1={CY} x2={RX} y2={CY} stroke="rgba(255,255,255,0.15)" strokeWidth="1" />

          {/* Y-axis labels */}
          <text x={LEFT - 8} y={TOP + 14}
            fontSize="11" fill="rgba(255,255,255,0.50)" textAnchor="end">HIGH IMPACT</text>
          <text x={LEFT - 8} y={BY - 6}
            fontSize="11" fill="rgba(255,255,255,0.50)" textAnchor="end">LOW IMPACT</text>

          {/* X-axis labels */}
          <text x={LEFT} y={VH - 8} fontSize="11" fill="rgba(255,255,255,0.50)">LOW EFFORT</text>
          <text x={RX}   y={VH - 8} fontSize="11" fill="rgba(255,255,255,0.50)" textAnchor="end">HIGH EFFORT</text>

          {/* Bubbles only — no tooltip here */}
          {[...points].sort((a, b) => b.r - a.r).map(p => {
            const isSelected = p.o.id === selectedId;
            const isHover    = p.o.id === hoverId;

            const fill =
              isSelected ? 'rgba(77, 77, 137, 0.34)' :
              isHover    ? 'rgba(255,255,255,0.12)' :
                           'rgba(10,22,46,0.85)';
            const stroke =
              isSelected ? '#0D55D7' :
              isHover    ? 'rgba(255,255,255,0.50)' :
                           'rgb(90,110,145)';

            return (
              <g
                key={p.o.id}
                style={{ cursor: 'pointer' }}
                onMouseEnter={() => setHoverId(p.o.id)}
                onMouseLeave={() => setHoverId(null)}
                onClick={() => onSelect(p.o.id)}
              >
                <circle
                  cx={p.x} cy={p.y} r={p.r}
                  fill={fill} stroke={stroke}
                  strokeWidth={isSelected ? 2.5 : 1.5}
                  style={{ transition: 'fill 0.15s, stroke 0.15s' }}
                />
              </g>
            );
          })}

          {/* Quadrant labels — rendered after bubbles */}
          {[
            { x: LEFT + 14, y: TOP + 24, label: 'QUICK WINS',        fill: 'rgba(255,255,255,0.60)', w: 102 },
            { x: CX + 14,   y: TOP + 24, label: 'HIGH VALUE',         fill: 'rgba(255,255,255,0.60)', w: 98  },
            { x: LEFT + 14, y: CY + 24,  label: 'LOW HANGING FRUIT',  fill: 'rgba(255,255,255,0.40)', w: 162 },
            { x: CX + 14,   y: CY + 24,  label: 'LONG TERM',          fill: 'rgba(255,255,255,0.40)', w: 94  },
          ].map(({ x, y, label, fill, w }) => (
            <g key={label} pointerEvents="none">
              <rect
                x={x - 6} y={y - 14}
                width={w} height={20}
                rx={3} fill="rgba(10,18,40,0.55)"
              />
              <text x={x} y={y}
                fontSize="11" fontWeight="700" letterSpacing="1.2" fill={fill}>
                {label}
              </text>
            </g>
          ))}

          {/* Tooltip labels — rendered LAST so always on top of everything */}
          {points.map(p => {
            const isSelected = p.o.id === selectedId;
            const isHover    = p.o.id === hoverId;
            if (!isHover && !isSelected) return null;
            const title = p.o.title.length > 26 ? p.o.title.slice(0, 26) + '…' : p.o.title;
            return (
              <text
                key={`label-${p.o.id}`}
                x={p.x}
                y={p.y - p.r - 7}
                fontSize="10"
                fill={isSelected ? '#0D55D7' : 'rgba(255,255,255,0.80)'}
                textAnchor="middle"
                pointerEvents="none"
              >
                {title}
              </text>
            );
          })}

        </svg>
      </div>

      <div className="mt-3 text-xs text-muted">
        Tip: Use this map to shortlist. Decisions and overrides happen in Analyst Review.
      </div>
    </div>
  );
}