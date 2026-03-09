import React from 'react';
import { Connector } from '../../types/connector';
import Badge from '../common/Badge';
import Button from '../common/Button';

const connectorIcons: Record<string, string> = {
  "ServiceNow": "bi-gear-fill",
  "Jira & Confluence": "bi-kanban-fill",
  "Microsoft 365": "bi-microsoft"
};

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

  const primaryLabel =
    isConnected
      ? 'Configure & Sync'
      : connector.status === 'coming_soon'
      ? 'Coming soon'
      : 'Connect';

  return (

    <div
      onClick={onSelect}
      className={`
      cursor-pointer
      rounded-xl
      border
      ${selected ? 'border-accent/60 bg-panel2' : 'border-border bg-panel'}
      p-5
      shadow-sm
      hover:bg-panel2
      flex
      flex-col
      justify-between
      min-h-[240px]
      min-w-0
      overflow-hidden
      `}
    >

      {/* HEADER */}
      <div className="flex items-start justify-between gap-3 min-w-0">

        <div className="min-w-0">

          {/* ICON + TITLE */}
          <div className="flex items-center gap-2 text-base font-semibold text-text min-w-0">
            <i className={`bi ${connectorIcons[connector.name] || "bi-plug-fill"} text-muted`} />

            <span className="truncate">
              {connector.name}
            </span>

          </div>

          {/* CATEGORY */}
          <div className="mt-1 text-sm text-muted truncate">
            {connector.category}
          </div>

        </div>

        <Badge status={connector.status} />

      </div>

      {/* METRICS */}
      <div className="mt-4 grid grid-cols-2 gap-4">

        {connector.metrics.slice(0, 2).map((m) => (

          <div
            key={m.label}
            className="
            rounded-lg
            border
            border-border
            bg-bg/30
            p-3
            min-w-0
            "
          >

            <div className="text-xs text-muted truncate">
              {m.label}
            </div>

            <div className="mt-1 text-lg font-semibold text-text truncate">
              {m.value}
            </div>

          </div>

        ))}

      </div>

      {/* SIGNAL + SYNC */}
      <div className="mt-4 flex items-center justify-between text-xs text-muted flex-wrap gap-2">

        <div className="truncate">
          Last synced:{' '}
          <span className="text-text">
            {isConnected ? connector.lastSynced : '—'}
          </span>
        </div>

        <div>
          Signal:{' '}
          <span className="text-text">
            {connector.signalStrength}
          </span>
        </div>

      </div>

      {/* BUTTONS */}
      <div className="mt-5 flex flex-wrap gap-3">

       <Button
  onClick={(e: React.MouseEvent) => {
    e.stopPropagation();
    onPrimary();
  }}
  disabled={connector.status === 'coming_soon'}
  variant="primary"
  className="flex-1 min-w-[120px]"
>
  {primaryLabel}
</Button>

<Button
  onClick={(e: React.MouseEvent) => {
    e.stopPropagation();
    onSecondary();
  }}
  variant="secondary"
  className="flex-1 min-w-[120px]"
  disabled={!isConnected}
  title={!isConnected ? 'Connect to enable data preview' : undefined}
>
  View data
</Button>

      </div>

    </div>
  );
}