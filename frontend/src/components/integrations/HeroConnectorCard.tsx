import React, { useEffect, useMemo, useState } from 'react';
import { Settings } from 'lucide-react';
import { Connector } from '../../types/connector';
import Badge from '../common/Badge';
import Button from '../common/Button';
import { connectorIcons } from './ConnectorIcons';

function parseMetricTarget(value: string) {
  const trimmed = value.trim();
  if (!/^[\d,]+$/.test(trimmed)) return null;

  const target = Number(trimmed.replace(/,/g, ''));
  return Number.isFinite(target) ? target : null;
}

function AnimatedMetricValue({
  value,
  active,
  animationKey = 0,
  delayMs = 0,
}: {
  value: string;
  active: boolean;
  animationKey?: number;
  delayMs?: number;
}) {
  const target = useMemo(() => parseMetricTarget(value), [value]);
  const [display, setDisplay] = useState(active ? value : '0');

  useEffect(() => {
    if (!active) {
      setDisplay('0');
      return;
    }

    if (target === null || animationKey <= 0) {
      setDisplay(value);
      return;
    }

    const prefersReducedMotion =
      typeof window !== 'undefined' &&
      typeof window.matchMedia === 'function' &&
      window.matchMedia('(prefers-reduced-motion: reduce)').matches;

    if (prefersReducedMotion) {
      setDisplay(value);
      return;
    }

    let frameId = 0;
    let timeoutId = 0;
    let startTime: number | null = null;
    const duration = 1150 + Math.min(450, value.length * 70);
    const hasCommaFormatting = value.includes(',');
    const format = (n: number) => hasCommaFormatting ? n.toLocaleString('en-US') : String(n);

    setDisplay('0');

    const tick = (time: number) => {
      if (startTime === null) startTime = time;
      const progress = Math.min(1, (time - startTime) / duration);
      const eased = 1 - Math.pow(1 - progress, 3);
      const nextValue = Math.round(target * eased);

      setDisplay(progress >= 1 ? value : format(nextValue));

      if (progress < 1) {
        frameId = window.requestAnimationFrame(tick);
      }
    };

    timeoutId = window.setTimeout(() => {
      frameId = window.requestAnimationFrame(tick);
    }, delayMs);

    return () => {
      window.clearTimeout(timeoutId);
      if (frameId) window.cancelAnimationFrame(frameId);
    };
  }, [active, animationKey, delayMs, target, value]);

  const metricWidth = `${Math.max(value.length, display.length)}ch`;

  return (
    <span
      aria-label={active ? value : '0'}
      className="metric-count-live"
      style={{ '--metric-value-width': metricWidth } as React.CSSProperties}
    >
      {display}
    </span>
  );
}

export default function HeroConnectorCard({
  connector,
  selected,
  onSelect,
  onPrimary,
  onSecondary,
  metricAnimationKey = 0,
}: {
  connector: Connector;
  selected: boolean;
  onSelect: () => void;
  onPrimary: () => void;
  onSecondary: () => void;
  metricAnimationKey?: number;
}) {
  const isConnected = connector.status === 'connected';
  const isConfigured = connector.configured;
  const primaryLabel =
    isConnected && isConfigured ? 'Re-sync'
    : isConnected ? 'Configure & Sync'
    : connector.status === 'coming_soon' ? 'Coming soon'
    : 'Connect';

  return (
    <div
      onClick={onSelect}
      className={`
      flex min-h-[240px] min-w-0 cursor-pointer flex-col justify-between overflow-hidden rounded-xl border-2
      ${selected ? 'border-accent bg-panel2' : 'border-border bg-panel'}
      p-5 shadow-sm hover:border-accent/40 hover:bg-panel2
    `}
    >
      <div className="min-w-0">
        <div className="flex min-w-0 items-center gap-2 text-base font-semibold text-text">
          <span className="shrink-0">{connectorIcons[connector.name] || <Settings size={18} className="text-slate-500" />}</span>
          <span className="leading-snug">{connector.name}</span>
        </div>
        <div className="mt-1 flex items-center justify-between gap-2">
          <div className="truncate text-sm text-muted">{connector.category}</div>
          <div className="shrink-0"><Badge status={connector.status} /></div>
        </div>
      </div>
      <div className="mt-4 grid grid-cols-2 gap-4">
        {connector.metrics.slice(0, 2).map((m, index) => (
          <div key={m.label} className="min-w-0 rounded-lg border border-border bg-bg/30 p-3">
            <div className="truncate text-xs text-muted">{m.label}</div>
            <div className="mt-1 h-7 overflow-hidden truncate text-lg font-semibold text-text">
              <AnimatedMetricValue
                value={m.value}
                active={isConfigured}
                animationKey={metricAnimationKey}
                delayMs={index * 130}
              />
            </div>
          </div>
        ))}
      </div>
      <div className="mt-4 flex flex-wrap items-center justify-between gap-2 text-xs text-muted">
        <div className="truncate">
          Last synced: <span className="text-text">{isConfigured ? connector.lastSynced : '—'}</span>
        </div>
        <div>
          Signal: <span className="text-text">{connector.signalStrength}</span>
        </div>
      </div>
      <div className="mt-5 flex flex-wrap gap-3">
        <Button
          onClick={(e: React.MouseEvent<HTMLButtonElement>) => {
            e.stopPropagation();
            onPrimary();
          }}
          disabled={connector.status === 'coming_soon'}
          variant="primary"
          className="min-w-[120px] flex-1"
        >
          {primaryLabel}
        </Button>
        <Button
          onClick={(e: React.MouseEvent<HTMLButtonElement>) => {
            e.stopPropagation();
            onSecondary();
          }}
          variant="secondary"
          className={`min-w-[120px] flex-1 ${isConnected ? '!border-[#0D55D7]/50 !text-[#0D55D7]' : ''}`}
          disabled={!isConnected}
          title={!isConnected ? 'Connect to enable data preview' : undefined}
        >
          View data
        </Button>
      </div>
    </div>
  );
}
