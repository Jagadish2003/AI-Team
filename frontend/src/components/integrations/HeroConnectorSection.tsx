import React from 'react';
import { Connector } from '../../types/connector';
import HeroConnectorCard from './HeroConnectorCard';
import { DISCOVERY_SOURCE_REQUIREMENT_MESSAGE } from '../../utils/sourceReadiness';

export default function HeroConnectorSection({
  connectors,
  selectedId,
  onSelect,
  onPrimary,
  onSecondary
}: {
  connectors: Connector[];
  selectedId: string | null;
  onSelect: (id: string) => void;
  onPrimary: (id: string) => void;
  onSecondary: (id: string) => void;
}) {
  return (
    <div>
      <div className="mb-4">
        <div className="text-xl font-semibold text-text">Connection Hub</div>
        <div className="text-xs text-muted pb-3 pt-3">{DISCOVERY_SOURCE_REQUIREMENT_MESSAGE}</div>
      </div>
      <div className="grid grid-cols-1 gap-6 md:grid-cols-3">
        {connectors.map((c) => (
          <HeroConnectorCard
            key={c.id}
            connector={c}
            selected={selectedId === c.id}
            onSelect={() => onSelect(c.id)}
            onPrimary={() => onPrimary(c.id)}
            onSecondary={() => onSecondary(c.id)}
          />
        ))}
      </div>
    </div>
  );
}
