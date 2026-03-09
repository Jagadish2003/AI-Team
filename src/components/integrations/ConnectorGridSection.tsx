import React from 'react';
import { Connector } from '../../types/connector';
import ConnectorTile from './ConnectorTile';

const connectorIcons: Record<string, string> = {
  "ServiceNow": "bi-gear-fill",
  "Jira & Confluence": "bi-kanban-fill",
  "Microsoft 365": "bi-microsoft",
  "SAP": "bi-box-seam",
  "GitHub": "bi-github",
  "Slack": "bi-chat-dots-fill",
  "Databricks": "bi-database-fill"
};

export default function ConnectorGridSection({
  connectors, selectedId, onSelect, onPrimary
}: { connectors: Connector[]; selectedId: string | null; onSelect:(id:string)=>void; onPrimary:(id:string)=>void }) {
  return (
    <div className="mt-1 pb-2 pb-[-190px]">
        <div className="text-sm font-semibold text-text">Add more coverage</div>
        <div className="text-xs text-muted">Add sources to improve confidence and evidence coverage.</div>

      <div className="grid grid-cols-1 md:grid-cols-3 gap-x-4 gap-y-6">
        {connectors.map(c => (
          <ConnectorTile
            key={c.id}
            connector={c}
            icon={connectorIcons[c.name] || "bi-plug-fill"}
            selected={selectedId===c.id}
            onSelect={()=>onSelect(c.id)}
            onPrimary={()=>onPrimary(c.id)}
          />
        ))}
      </div>
    </div>
  );
}