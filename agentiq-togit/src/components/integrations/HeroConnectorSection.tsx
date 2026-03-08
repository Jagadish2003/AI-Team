import React from 'react';
import { Connector } from '../../types/connector';
import HeroConnectorCard from './HeroConnectorCard';

export default function HeroConnectorSection({
  connectors,
  selectedId,
  onSelect,
  onPrimary,
  onSecondary,
}: {
  connectors: Connector[];
  selectedId: string | null;
  onSelect: (id: string) => void;
  onPrimary: (id: string) => void;
  onSecondary: (id: string) => void;
}) {
  return (
    <section className="rounded-xl border border-border bg-panel p-5 shadow-panel">
      <div className="border-b border-border pb-4">
        <div className="text-[18px] font-semibold text-text">Start here <span className="font-normal text-text/80">(fastest to value)</span></div>
        <div className="mt-1 text-[15px] text-muted">Connect 1 to start discovery</div>
      </div>

      <div className="mt-4 grid grid-cols-1 gap-3 xl:grid-cols-3">
        {connectors.map((connector) => (
          <HeroConnectorCard
            key={connector.id}
            connector={connector}
            selected={selectedId === connector.id}
            onSelect={() => onSelect(connector.id)}
            onPrimary={() => onPrimary(connector.id)}
            onSecondary={() => onSecondary(connector.id)}
          />
        ))}
      </div>
    </section>
  );
}
