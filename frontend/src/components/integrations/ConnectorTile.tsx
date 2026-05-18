import React from 'react';
import { Connector } from '../../types/connector';
import Badge from '../common/Badge';
import Button from '../common/Button';

export default function ConnectorTile({
  connector,
  icon,
  selected,
  onSelect,
  onPrimary
}: {
  connector: Connector;
  icon: React.ReactNode;
  selected: boolean;
  onSelect: () => void;
  onPrimary: () => void;
}) {
  const isConnected = connector.status === 'connected';
  const isConfigured = connector.configured;
  const actionLabel = isConnected && !isConfigured ? 'Configure & Sync' : isConnected ? 'View data' : 'Connect';

  return (
    <div
      onClick={onSelect}
      className={`connector-card flex h-[215px] cursor-pointer flex-col rounded-xl border ${
        selected ? 'connector-card-selected' : 'border-border bg-panel'
      } p-4 hover:border-accent/60 hover:bg-panel2`}
    >
      {/* Header */}
      <div className="min-w-0">
        <div className="flex items-center gap-2 text-base font-semibold text-text">
          <span className="shrink-0">{icon}</span>
          <span className="leading-snug">{connector.name}</span>
        </div>
        <div className="mt-0.5 flex items-center justify-between gap-2">
          <div className="truncate text-xs text-muted">{connector.category}</div>
          <div className="shrink-0"><Badge status={connector.status} /></div>
        </div>
      </div>

      {/* Tags */}
      <div className="mt-2 flex flex-col items-start gap-1">
        {connector.reads.slice(0, 2).map((r) => (
          <span
            key={r}
            className="rounded-md border border-border bg-bg/30 px-2 py-0.5 text-[11px] text-muted"
          >
            {r}
          </span>
        ))}
      </div>

      {/* Signal */}
      <div className="mt-2 flex items-center justify-between text-xs text-muted">
        <span>
          Signal: <span className="text-text">{connector.signalStrength}</span>
        </span>
        <span>{isConfigured ? `Synced ${connector.lastSynced}` : '—'}</span>
      </div>

      {/* Button */}
      <div className="mt-auto pb-1 pt-4">
        <Button
          variant={isConnected ? 'secondary' : 'primary'}
          className={`w-full ${
            isConnected
              ? 'light-view-data-button !border-accent/50 !text-accent'
              : ''
          }`}
          onClick={(e: React.MouseEvent<HTMLButtonElement>) => {
            e.stopPropagation();
            onPrimary();
          }}
        >
          {actionLabel}
        </Button>
      </div>
    </div>
  );
}
