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
  const actionLabel = isConnected ? 'View data' : 'Connect';

  return (
    <div
      onClick={onSelect}
      className={`h-[190px] cursor-pointer rounded-xl border-2 ${
        selected ? 'border-accent bg-panel2' : 'border-border bg-panel'
      } p-4 hover:border-accent/60 hover:bg-panel2`}
    >
      {/* Header */}
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="flex items-center gap-2 text-base font-semibold text-text">
            {icon}
            {connector.name}
          </div>
          <div className="mt-0.5 truncate text-xs text-muted">
            {connector.category}
          </div>
        </div>
        <Badge status={connector.status} />
      </div>

      {/* Tags */}
      <div className="mt-2 flex flex-wrap gap-1">
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
      <div className="mt-5 pb-1">
        <Button
          variant={isConnected ? 'secondary' : 'primary'}
          className={`w-full ${
            isConnected
              ? '!text-[#00B4B4] !border-[#00B4B4]/50'
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