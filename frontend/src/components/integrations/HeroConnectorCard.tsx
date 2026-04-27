import React from 'react';
import { Settings } from 'lucide-react';
import { Connector } from '../../types/connector';
import Badge from '../common/Badge';
import Button from '../common/Button';
import { connectorIcons } from './ConnectorIcons';

export default function HeroConnectorCard({
  connector,
  selected,
  onSelect,
  onPrimary,
  onSecondary
}: {
  connector: Connector;
  selected: boolean;
  onSelect: () => void;
  onPrimary: () => void;
  onSecondary: () => void;
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
      <div className="flex min-w-0 items-center justify-between gap-2">
        <div className="min-w-0 flex-1">
          <div className="flex min-w-0 items-center gap-2 text-base font-semibold text-text">
            <span className="shrink-0">{connectorIcons[connector.name] || <Settings size={18} className="text-slate-500" />}</span>
            <span className="min-w-0 break-words leading-tight">{connector.name}</span>
          </div>
          <div className="mt-1 truncate text-sm text-muted">{connector.category}</div>
        </div>
        <div className="shrink-0 self-start">
          <Badge status={connector.status} />
        </div>
      </div>
      <div className="mt-4 grid grid-cols-2 gap-4">
        {connector.metrics.slice(0, 2).map((m) => (
          <div key={m.label} className="min-w-0 rounded-lg border border-border bg-bg/30 p-3">
            <div className="truncate text-xs text-muted">{m.label}</div>
            <div className="mt-1 truncate text-lg font-semibold text-text">
              {isConfigured ? m.value : '—'}
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
          className={`min-w-[120px] flex-1 ${isConnected ? '!border-[#00B4B4]/50 !text-[#00B4B4]' : ''}`}
          disabled={!isConnected}
          title={!isConnected ? 'Connect to enable data preview' : undefined}
        >
          View data
        </Button>
      </div>
    </div>
  );
}