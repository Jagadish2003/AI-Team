import React from 'react';
import { Connector } from '../../types/connector';
import Badge from '../common/Badge';
import Button from '../common/Button';
import { accessIcons } from './AccessIcons';
import { useToast } from '../common/Toast';
import { ExternalLink, CheckCircle2 } from 'lucide-react';

// ── T41-7: Connection Health — configured read scope for this connector.
// Shows what AgentIQ is configured to read from this source.
// Derived deterministically from connector.id (CONNECTION_HEALTH_LABELS map).
// IMPORTANT: reflects configured read scope, NOT proven last-sync results.
// Sprint 6 will wire this to real sync telemetry (Connection Health v2).

const CONNECTION_HEALTH_LABELS: Record<string, string[]> = {
  salesforce: [
    'Read Case records',
    'Read Flow metadata',
    'Read Approval history',
    'Read User records',
    'Read OpportunityLineItem records',
  ],
  servicenow: [
    'Read Incident records',
    'Read Change Request records',
    'Read SLA definitions',
  ],
  jira: [
    'Read Issue records',
    'Read Project configuration',
    'Read Sprint data',
  ],
  jira_confluence: [
    'Read Issue records',
    'Read Project configuration',
    'Read Space content',
    'Read Page metadata',
  ],
  confluence: [
    'Read Space content',
    'Read Page metadata',
  ],
  ncino: [
    'Read LLC_BI__Loan__c records',
    'Read LLC_BI__Covenant__c records',
    'Read LLC_BI__Spreading__c records',
  ],
};

function ConnectionHealthSection({ connector }: { connector: Connector }) {
  if (connector.status !== 'connected') return null;

  // Use connector-specific health labels, falling back to reads array
  const items =
    CONNECTION_HEALTH_LABELS[connector.id] ??
    connector.reads.map((r) => `Read ${r} records`);

  return (
    <div className="mt-4">
      <div className="mb-2 flex items-center justify-between gap-2">
        <div className="text-sm font-medium text-text">Connection Health</div>
        <div className="text-[10px] font-medium uppercase tracking-wide text-muted">
          Configured Read Scope
        </div>
      </div>
      <div className="space-y-1.5">
        {items.map((label) => (
          <div
            key={label}
            className="flex items-center gap-2 rounded-md border border-border bg-bg/20 px-3 py-2 text-xs text-text"
          >
            <CheckCircle2
              size={14}
              className="shrink-0 text-emerald-400"
            />
            <span className="flex-1">{label}</span>
            <span className="text-emerald-400 text-[10px] font-medium">✓</span>
          </div>
        ))}
      </div>
      <p className="mt-2 text-[10px] text-muted leading-relaxed">
        Configured read scope for this connector. Actual sync results available in Sprint 6.
      </p>
    </div>
  );
}

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

      {/* T41-7: Connection Health — shown only when connected */}
      <ConnectionHealthSection connector={connector} />

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
