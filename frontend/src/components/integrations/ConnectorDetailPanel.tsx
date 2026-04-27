import React from 'react';
import { Connector } from '../../types/connector';
import Badge from '../common/Badge';
import Button from '../common/Button';
import { accessIcons } from './AccessIcons';
import { useToast } from '../common/Toast';
import { ExternalLink } from 'lucide-react';

export default function ConnectorDetailPanel({
  connector,
  onConfigure
}: {
  connector: Connector | null;
  onConfigure: () => void;
}) {
  const { push } = useToast(); 

  if (!connector) {
    return (
      <div className="rounded-xl border border-border bg-panel p-4 text-sm text-muted">
        Select a connector to view details.
      </div>
    );
  }

  const isConnected = connector.status === 'connected';
  const isConfigured = connector.configured;

  return (
    <div className="rounded-xl border border-border bg-panel p-5">
      
      {/* Header */}
      <div className="flex items-start justify-between gap-2">
        <div>
          <div className="text-xl font-semibold text-text">
            {connector.name} Integration
          </div>
          <div className="mt-1 text-sm text-muted">
            {connector.category}
          </div>
        </div>

        <Badge status={connector.status} />
      </div>

      {/* Last Sync + Learn More */}
      <div className="mt-3 flex items-center justify-between text-xs text-muted">
        <div>
          Last sync:{' '}
          <span className="text-text">
            {isConfigured ? connector.lastSynced : '—'}
          </span>
        </div>

        {/* Toast on click */}
        <button
          onClick={() => push('More details available in later sprint.')}
          className="flex items-center gap-1 text-accent hover:underline"
        >
          Learn More <ExternalLink size={14} />
        </button>
      </div>

      <div className="mt-4 border-t border-border" />

      {/* Access Section */}
      <div className="mt-4">
        <div className="mb-2 text-sm font-medium text-text">
          Access as:
        </div>

        <div className="space-y-2">
          {connector.reads.slice(0, 3).map((r) => (
            <div
              key={r}
              className="flex items-center justify-between rounded-md border border-border px-3 py-2 hover:bg-panel2"
            >
              <div className="flex items-center gap-2 text-sm text-text">
                <div className="flex h-5 w-5 items-center justify-center rounded bg-accent/20">
                  {accessIcons[r] || accessIcons.fallback}
                </div>
                {r}
              </div>

              <span className="text-muted">›</span>
            </div>
          ))}
        </div>
      </div>

      {/* CTA */}
      <div className="mt-5">
        <Button
          variant="primary"
          className="w-full whitespace-nowrap"
          onClick={onConfigure}
          disabled={!isConnected || connector.status === 'coming_soon'}
          title={!isConnected ? 'Connect this source first' : undefined}
        >
          {isConfigured ? 'Re-sync' : 'Configure & Sync'}
        </Button>
      </div>

    </div>
  );
}