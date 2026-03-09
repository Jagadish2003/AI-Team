import React from 'react';
import { Connector } from '../../types/connector';
import HeroConnectorCard from './HeroConnectorCard';

export default function HeroConnectorSection({
  connectors, selectedId, onSelect, onPrimary, onSecondary
}: { connectors: Connector[]; selectedId: string | null; onSelect:(id:string)=>void; onPrimary:(id:string)=>void; onSecondary:(id:string)=>void }) {
  return (
    <div>
      <div className="mb-3">
        <div className="text-sm font-semibold text-text">Start here (fastest to value)</div>
        <div className="text-xs text-muted">Connect at least 1 source to start discovery.</div>
      </div>
      <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
        {connectors.map(c => (
          <HeroConnectorCard key={c.id} connector={c} selected={selectedId===c.id} onSelect={()=>onSelect(c.id)} onPrimary={()=>onPrimary(c.id)} onSecondary={()=>onSecondary(c.id)} />
        ))}
      </div>
    </div>
  );
}