import React from 'react';
import { Connector } from '../../types/connector';
import Button from '../common/Button';

function ConnectorBadge({ connector }: { connector: Connector }) {
  if (connector.status === 'connected') {
    return (
      <span className="inline-flex items-center gap-2 rounded-md bg-[#dcdcdf] px-3 py-1.5 text-sm font-medium text-text">
        <span className="text-[13px]">✔</span>
        Connected
      </span>
    );
  }

  return null;
}

function IconChip({ name }: { name: string }) {
  const map: Record<string, string> = {
    ServiceNow: '⚙',
    'Jira & Confluence': '◫',
    'Microsoft 365': '⬡',
  };

  return (
    <div className="inline-flex h-10 w-10 items-center justify-center rounded-lg bg-panel2 text-lg text-muted">
      {map[name] ?? '◻'}
    </div>
  );
}

function metricLine(value: string, label: string) {
  if (!value) {
    return label;
  }

  return (
    <>
      <span className="font-semibold text-text">{value}</span> {label}
    </>
  );
}

export default function HeroConnectorCard({
  connector,
  selected,
  onSelect,
  onPrimary,
  onSecondary,
}: {
  connector: Connector;
  selected: boolean;
  onSelect: () => void;
  onPrimary: () => void;
  onSecondary: () => void;
}) {
  const primaryLabel = connector.name === 'Jira & Confluence' ? 'Configure & Sync' : connector.status === 'connected' ? 'Configure & Sync' : 'Connect';

  return (
    <div
      onClick={onSelect}
      className={`rounded-xl border p-4 transition ${
        selected ? 'border-[#bfc0c5] bg-white shadow-panel' : 'border-border bg-panel hover:border-[#c7c7cc]'
      }`}
    >
      <div className="flex items-start justify-between gap-3">
        <div className="flex items-center gap-3">
          <IconChip name={connector.name} />
          <div>
            <div className="text-[18px] font-semibold text-text">{connector.name}</div>
          </div>
        </div>
        <ConnectorBadge connector={connector} />
      </div>

      <div className="mt-5 space-y-3 text-[15px] text-muted">
        {connector.metrics.slice(0, 2).map((metric) => (
          <div key={`${connector.id}-${metric.label}`} className="flex items-center gap-3">
            <span className="h-2.5 w-2.5 rounded-full bg-[#b9bac1]" />
            <span>{metricLine(metric.value, metric.label)}</span>
          </div>
        ))}
      </div>

      <div className="mt-6 flex gap-3">
        <Button
          onClick={(event) => {
            event.stopPropagation();
            onPrimary();
          }}
          variant="secondary"
          className="flex-1"
        >
          {primaryLabel}
        </Button>
        <Button
          onClick={(event) => {
            event.stopPropagation();
            onSecondary();
          }}
          variant="secondary"
          className="flex-1"
          disabled={connector.status !== 'connected'}
          title={connector.status !== 'connected' ? 'Connect first to enable data preview' : undefined}
        >
          View data
        </Button>
      </div>
    </div>
  );
}
