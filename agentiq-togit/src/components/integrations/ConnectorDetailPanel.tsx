import React from 'react';
import { Connector } from '../../types/connector';
import Button from '../common/Button';

export default function ConnectorDetailPanel({
  connector,
  onConfigure,
}: {
  connector: Connector | null;
  onConfigure: () => void;
}) {
  if (!connector) {
    return <div className="rounded-xl border border-border bg-panel p-5 shadow-panel">Select a connector to view details.</div>;
  }

  return (
    <section className="rounded-xl border border-border bg-panel shadow-panel">
      <div className="p-5">
        <div className="flex items-start justify-between gap-3">
          <div>
            <div className="text-[18px] font-semibold text-text">{connector.name} Integration</div>
            <div className="mt-3 inline-flex items-center gap-2 rounded-md bg-[#dcdcdf] px-3 py-1.5 text-sm font-medium text-text">
              <span>✔</span>
              {connector.status === 'connected' ? 'Connected' : 'Not connected'}
            </div>
          </div>
          <div className="pt-3 text-[15px] text-muted">{connector.lastSynced}</div>
        </div>

        <button className="mt-4 inline-flex items-center gap-2 text-[15px] text-muted hover:text-text">
          Learn More <span>↗</span>
        </button>
      </div>

      <div className="border-t border-border px-5 py-4">
        <div className="text-[15px] font-medium text-text">Access as:</div>
        <div className="mt-3 divide-y divide-border">
          {connector.reads.slice(0, 3).map((item) => (
            <div key={item} className="flex items-center justify-between py-3 text-[15px] text-text">
              <div className="flex items-center gap-3">
                <span className="inline-flex h-8 w-8 items-center justify-center rounded-md bg-panel2 text-sm text-muted">▣</span>
                {item}
              </div>
              <span className="text-xl text-muted">›</span>
            </div>
          ))}
        </div>
      </div>

      <div className="px-5 pb-5 pt-1">
        <Button variant="secondary" className="w-full" onClick={onConfigure}>
          Configure & Sync ↗
        </Button>
      </div>
    </section>
  );
}
