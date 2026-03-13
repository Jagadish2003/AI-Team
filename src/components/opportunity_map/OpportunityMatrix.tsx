import React, { useState } from 'react';
import { OpportunityCandidate } from '../../types/analystReview';

function clamp(n: number, a: number, b: number) { return Math.max(a, Math.min(b, n)); }

function buildPoints(filtered: OpportunityCandidate[]) {
  const W = 640, H = 440, pad = 40;
  const rScore = (o: OpportunityCandidate) => o.impact - o.effort;
  return filtered.map(o => {
    const x = pad + ((o.effort - 1) / 9) * (W - 2 * pad);
    const y = (H - pad) - ((o.impact - 1) / 9) * (H - 2 * pad);
    const r = clamp(8 + rScore(o) * 2, 6, 22);
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
      <div className="flex items-center justify-between">
        <div className="text-sm font-semibold text-text">Effort vs Impact</div>
        <div className="text-xs text-muted">Click a bubble to preview details</div>
      </div>

      <div className="mt-3 overflow-auto rounded-lg border border-border bg-bg/10 p-3">
        <svg width="720" height="520" viewBox="0 0 720 520">
          <rect x="40" y="40" width="640" height="440" fill="none" stroke="rgba(255,255,255,0.12)" />
          <line x1="360" y1="40" x2="360" y2="480" stroke="rgba(255,255,255,0.10)" />
          <line x1="40" y1="260" x2="680" y2="260" stroke="rgba(255,255,255,0.10)" />

          <text x="40"  y="505" fontSize="12" fill="rgba(255,255,255,0.60)">Low Effort</text>
          <text x="640" y="505" fontSize="12" fill="rgba(255,255,255,0.60)">High Effort</text>
          <text x="8"   y="260" transform="rotate(-90 8 260)" fontSize="12" fill="rgba(255,255,255,0.60)">Impact</text>
          <text x="340" y="18"  fontSize="12" fill="rgba(255,255,255,0.60)">High Impact</text>
          <text x="340" y="515" fontSize="12" fill="rgba(255,255,255,0.60)">Low Impact</text>

          <text x="60"  y="70"  fontSize="12" fill="rgba(255,255,255,0.70)">QUICK WINS</text>
          <text x="390" y="70"  fontSize="12" fill="rgba(255,255,255,0.70)">HIGH VALUE</text>
          <text x="60"  y="450" fontSize="12" fill="rgba(255,255,255,0.55)">LOW HANGING FRUIT</text>
          <text x="390" y="450" fontSize="12" fill="rgba(255,255,255,0.55)">LONG TERM</text>

          {points.map(p => {
            const isSelected = p.o.id === selectedId;
            const isHover    = p.o.id === hoverId;
            const fill   = isSelected ? 'rgba(0,180,180,0.25)' : isHover ? 'rgba(255,255,255,0.12)' : 'rgba(255,255,255,0.07)';
            const stroke = isSelected ? '#00B4B4'              : isHover ? 'rgba(255,255,255,0.40)' : 'rgba(255,255,255,0.20)';
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
                  fill={fill} stroke={stroke} strokeWidth={isSelected ? 2 : 1}
                  style={{ transition: 'fill 0.15s, stroke 0.15s' }}
                />
              </g>
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
