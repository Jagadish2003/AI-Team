import React from 'react';
import { Connector } from '../../types/connector';
import ConnectorTile from './ConnectorTile';

export default function ConnectorGridSection({
  connectors,
  selectedId,
  onSelect,
  onPrimary,
}: {
  connectors: Connector[];
  selectedId: string | null;
  onSelect: (id: string) => void;
  onPrimary: (id: string) => void;
}) {
  return (
    <section className="mt-5">
      <div className="mb-4 text-[16px] font-semibold text-text">Add more coverage</div>
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-4">
        {connectors.map((connector) => (
          <ConnectorTile
            key={connector.id}
            connector={connector}
            selected={selectedId === connector.id}
            onSelect={() => onSelect(connector.id)}
            onPrimary={() => onPrimary(connector.id)}
          />
        ))}
      </div>
    </section>
  );
}
