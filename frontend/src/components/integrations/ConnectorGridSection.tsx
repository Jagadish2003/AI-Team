import React from 'react';
import { Connector } from '../../types/connector';
import ConnectorTile from './ConnectorTile';
import { connectorIcons, fallbackConnectorIcon } from './ConnectorIcons';

export default function ConnectorGridSection({
  connectors,
  selectedId,
  onSelect,
  onPrimary
}: {
  connectors: Connector[];
  selectedId: string | null;
  onSelect: (id: string) => void;
  onPrimary: (id: string) => void;
}) {
  return (
    <div>
      <div className="mb-5 space-y-2">
        <div className="text-xl font-semibold leading-tight text-text">Add more coverage</div>
        <div className="max-w-3xl text-sm leading-relaxed text-muted">Add sources to improve confidence and evidence coverage.</div>
      </div>

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {connectors.map((c) => (
          <ConnectorTile
            key={c.id}
            connector={c}
            icon={connectorIcons[c.name] || fallbackConnectorIcon}
            selected={selectedId === c.id}
            onSelect={() => onSelect(c.id)}
            onPrimary={() => onPrimary(c.id)}
          />
        ))}
      </div>
    </div>
  );
}
