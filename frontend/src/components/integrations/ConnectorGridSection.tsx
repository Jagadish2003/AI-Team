import React from 'react';
import { Plug } from 'lucide-react';
import { Connector } from '../../types/connector';
import ConnectorTile from './ConnectorTile';
import { connectorIcons } from './ConnectorIcons';

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
    <div className="mt-1 pb-2">
      <div className="text-xl font-semibold text-text">Add more coverage</div>
      <div className="text-xs text-muted pb-3 pt-3">Add sources to improve confidence and evidence coverage.</div>

      <div className="grid grid-cols-1 gap-x-4 gap-y-6 md:grid-cols-3">
        {connectors.map((c) => (
          <ConnectorTile
            key={c.id}
            connector={c}
            icon={connectorIcons[c.name] || <Plug size={18} className="text-slate-500" />}
            selected={selectedId === c.id}
            onSelect={() => onSelect(c.id)}
            onPrimary={() => onPrimary(c.id)}
          />
        ))}
      </div>
    </div>
  );
}