import React from 'react';
import { OpportunityCandidate } from '../../types/analystReview';

const Q     = 220;
const LEFT  = 90;
const RIGHT = 20;
const TOP   = 20;
const BOT   = 32;

const VW = LEFT + Q * 2 + RIGHT;
const VH = TOP  + Q * 2 + BOT;

const CX = LEFT + Q;
const CY = TOP  + Q;
const RX = VW - RIGHT;
const BY = VH - BOT;

function clamp(n: number, a: number, b: number) {
  return Math.max(a, Math.min(b, n));
}

function buildPoints(opportunities: OpportunityCandidate[]) {
  const W = RX - LEFT;
  const H = BY - TOP;
  return opportunities.map(o => ({
    o,
    x: LEFT + ((o.effort - 1) / 9) * W,
    y: BY   - ((o.impact - 1) / 9) * H,
    r: clamp(8 + o.impact * 1.2, 10, 20),
  }));
}

const QUADRANT_LABELS = [
  { x: LEFT + 10, y: TOP + 20, label: 'QUICK WINS',       fill: 'rgba(255,255,255,0.60)', w: 90  },
  { x: CX   + 10, y: TOP + 20, label: 'HIGH VALUE',        fill: 'rgba(255,255,255,0.60)', w: 84  },
  { x: LEFT + 10, y: CY + 20,  label: 'LOW HANGING FRUIT', fill: 'rgba(255,255,255,0.40)', w: 138 },
  { x: CX   + 10, y: CY + 20,  label: 'LONG TERM',         fill: 'rgba(255,255,255,0.40)', w: 80  },
] as const;

interface SnapshotMatrixProps {
  opportunities: OpportunityCandidate[];
}

export default function SnapshotMatrix({ opportunities }: SnapshotMatrixProps) {
  const points = buildPoints(opportunities).sort((a, b) => b.r - a.r);

  return (
    <div className="rounded-lg border border-border bg-bg/10 p-3">
      <div className="flex items-center justify-between mb-2">
        <div className="text-sm font-semibold text-text">
          EFFORT vs. IMPACT (snapshot)
        </div>
      </div>

      {/* Centered + scalable container */}
      <div className="rounded-lg border border-border bg-bg/10 overflow-hidden flex justify-center">
        <svg
          viewBox={`0 0 ${VW} ${VH}`}
          className="w-full max-w-[620px] h-auto block"
          preserveAspectRatio="xMinYMin meet"
        >

          {/* ── Quadrant background fills ── */}
          <rect x={LEFT} y={TOP} width={CX - LEFT} height={CY - TOP} fill="rgba(0,180,180,0.03)" />
          <rect x={CX}   y={TOP} width={RX - CX}   height={CY - TOP} fill="rgba(255,255,255,0.01)" />
          <rect x={LEFT} y={CY}  width={CX - LEFT} height={BY - CY}  fill="rgba(255,255,255,0.01)" />
          <rect x={CX}   y={CY}  width={RX - CX}   height={BY - CY}  fill="rgba(255,255,255,0.01)" />

          {/* ── Outer border ── */}
          <rect
            x={LEFT} y={TOP} width={RX - LEFT} height={BY - TOP}
            fill="none" stroke="rgba(255,255,255,0.15)" strokeWidth="1"
          />

          {/* ── Quadrant dividers ── */}
          <line x1={CX}   y1={TOP} x2={CX} y2={BY} stroke="rgba(255,255,255,0.15)" strokeWidth="1" />
          <line x1={LEFT} y1={CY}  x2={RX} y2={CY} stroke="rgba(255,255,255,0.15)" strokeWidth="1" />

          {/* ── Y-axis labels ── */}
          <text x={LEFT - 6} y={TOP + 12} fontSize="9" fill="rgba(255,255,255,0.45)" textAnchor="end">
            HIGH IMPACT
          </text>
          <text x={LEFT - 6} y={BY - 4} fontSize="9" fill="rgba(255,255,255,0.45)" textAnchor="end">
            LOW IMPACT
          </text>

          {/* ── X-axis labels ── */}
          <text x={LEFT} y={VH - 8} fontSize="9" fill="rgba(255,255,255,0.45)">
            LOW EFFORT
          </text>
          <text x={RX} y={VH - 8} fontSize="9" fill="rgba(255,255,255,0.45)" textAnchor="end">
            HIGH EFFORT
          </text>

          {/* ── Bubbles ── */}
          {points.map(({ o, x, y, r }) => (
            <circle
              key={o.id}
              cx={x}
              cy={y}
              r={r}
              fill="rgba(10,22,46,0.85)"
              stroke="rgb(90,110,145)"
              strokeWidth="1.5"
            />
          ))}

          {/* ── Quadrant labels ── */}
          {QUADRANT_LABELS.map(({ x, y, label, fill, w }) => (
            <g key={label} pointerEvents="none">
              <rect
                x={x - 5} y={y - 13}
                width={w} height={17}
                rx={3} fill="rgba(10,18,40,0.55)"
              />
              <text x={x} y={y} fontSize="9" fontWeight="700" letterSpacing="1.1" fill={fill}>
                {label}
              </text>
            </g>
          ))}
        </svg>
      </div>
    </div>
  );
}