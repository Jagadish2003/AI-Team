import React from 'react';
import { Connector } from '../../types/connector';
import Button from '../common/Button';

function IconChip({ name }: { name: string }) {
  const map: Record<string, string> = {
    SAP: '◈',
    GitHub: '◼',
    Slack: '✚',
    Databricks: '⬒',
  };

  return (
    <div className="inline-flex h-9 w-9 items-center justify-center rounded-lg bg-panel2 text-base text-muted">
      {map[name] ?? '◻'}
    </div>
  );
}

export default function ConnectorTile({
  connector,
  selected,
  onSelect,
  onPrimary,
}: {
  connector: Connector;
  selected: boolean;
  onSelect: () => void;
  onPrimary: () => void;
}) {
  const isComingSoon = connector.status === 'coming_soon';
  const objectLabel = connector.metrics[0]?.value ?? connector.reads[0] ?? 'Object';

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
          <div className="text-[18px] font-medium text-text">{connector.name}</div>
        </div>
        {connector.name === 'Databricks' ? (
          <span className="rounded-md border border-border bg-panel2 px-2 py-1 text-[12px] font-medium text-muted">Regulated</span>
        ) : null}
      </div>

      {isComingSoon ? (
        <>
          <div className="mt-5 text-[16px] font-medium text-text">Coming soon</div>
          <div className="mt-2 text-[15px] leading-6 text-muted">{connector.metrics[0]?.value}</div>
          <div className="mt-5 flex gap-2">
            <span className="inline-flex h-10 items-center rounded-lg border border-border bg-[#e3e3e6] px-4 text-sm font-medium text-text/70">Regulated</span>
            <span className="inline-flex h-10 items-center rounded-lg border border-border bg-panel2 px-4 text-sm font-medium text-muted">Coming soon</span>
          </div>
          <div className="mt-3 flex items-center gap-2 text-sm text-muted">
            <span className="h-2 w-2 rounded-full bg-[#c3c4ca]" />
            <span className="h-2 w-2 rounded-full bg-[#d4d5db]" />
            {connector.reads[0]}
          </div>
        </>
      ) : (
        <>
          <div className="mt-6 flex items-center gap-2 text-[15px] text-muted">
            <span className="text-text">✓</span>
            Connected
          </div>
          <div className="mt-5 inline-flex items-center gap-2 rounded-md bg-panel2 px-3 py-2 text-[15px] text-text/85">
            <span className="h-3 w-3 rounded-sm bg-[#c8c9cf]" />
            {objectLabel}
          </div>
          <div className="mt-6">
            <Button
              variant="secondary"
              className="w-full"
              onClick={(event) => {
                event.stopPropagation();
                onPrimary();
              }}
            >
              ✓ View data
            </Button>
          </div>
        </>
      )}
    </div>
  );
}
