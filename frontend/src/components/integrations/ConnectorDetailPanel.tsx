import React from 'react';
import { Connector } from '../../types/connector';
import Badge from '../common/Badge';
import Button from '../common/Button';
import { accessIcons } from './AccessIcons';
import { useToast } from '../common/Toast';
import { ExternalLink, CheckCircle2 } from 'lucide-react';
import SalesforceProductPicker from './SalesforceProductPicker';
import SqlServerScopePicker from './SqlServerScopePicker';
import OracleScopePicker from './OracleScopePicker';
import PostgreSQLScopePicker from './PostgreSQLScopePicker';

// T41-7: Connection Health - configured read scope for this connector.
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
  salesforce_ncino: [
    'Read LLC_BI__Loan__c records',
    'Read LLC_BI__Covenant2__c records',
    'Read LLC_BI__Checklist__c records',
    'Read LLC_BI__Spread_Statement_Period__c records',
    'Read ProcessInstance records',
  ],
  salesforce_strs: [
    'Read IndividualApplication records',
    'Read BenefitAssignment records',
    'Read Case records (Disability)',
    'Read Program records',
    'Read Contact records',
  ],
  servicenow: [
    'Read Incident records',
    'Read benefit operations signals',
    'Read SLA definitions',
  ],
  jira_confluence: [
    'Read Issue records',
    'Read benefit operations signals',
    'Read Project configuration',
    'Read Space content',
  ],
  jira: [
    'Read Issue records',
    'Read benefit operations signals',
    'Read Sprint data',
  ],
  confluence: [
    'Read Space content',
    'Read Page metadata',
  ],
  ncino: [
    'Read LLC_BI__Loan__c records',
    'Read LLC_BI__Covenant2__c records',
    'Read LLC_BI__Spread_Statement_Period__c records',
  ],
};

function ConnectionHealthSection({ connector }: { connector: Connector }) {
  if (connector.status !== 'connected') return null;

  const healthKey =
    connector.id === 'salesforce' && connector.category?.includes('nCino')
      ? 'salesforce_ncino'
      : connector.id === 'salesforce' &&
          (connector.category?.includes('PSS') || connector.category?.includes('Benefits'))
        ? 'salesforce_strs'
        : connector.id;

  const items =
    CONNECTION_HEALTH_LABELS[healthKey] ??
    connector.reads.map((readScope) => `Read ${readScope} records`);

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
            <CheckCircle2 size={14} className="shrink-0 text-emerald-400" />
            <span className="min-w-0 flex-1 break-words">{label}</span>
          </div>
        ))}
      </div>
      <p className="mt-2 text-[10px] leading-relaxed text-muted">
        Configured read scope for this connector. Actual sync results available in Sprint 6.
      </p>
    </div>
  );
}

export default function ConnectorDetailPanel({
  connector,
  onConfigure,
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
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="break-words text-xl font-semibold leading-snug text-text">
            {connector.name} Integration
          </div>
          <div className="mt-1 break-words text-sm text-muted">{connector.category}</div>
        </div>

        <Badge status={connector.status} />
      </div>

      <div className="mt-3 flex items-center justify-between text-xs text-muted">
        <div>
          Last sync:{' '}
          <span className="text-text">
            {isConfigured ? connector.lastSynced : '-'}
          </span>
        </div>

        <button
          onClick={() => push('More details available in later sprint.')}
          className="inline-flex items-center gap-1 rounded-md border border-accent/20 bg-accent/5 px-2 py-1 text-xs font-medium text-accent transition-colors hover:border-accent/45 hover:bg-accent/10 focus:outline-none focus-visible:ring-1 focus-visible:ring-accent/40"
        >
          Learn More <ExternalLink size={14} />
        </button>
      </div>

      <div className="mt-4 border-t border-border" />

      <div className="mt-4">
        <div className="mb-2 text-sm font-medium text-text">Access as:</div>

        <div className="space-y-2">
          {connector.reads.slice(0, 3).map((readScope) => (
            <div
              key={readScope}
              className="flex items-center rounded-md border border-border px-3 py-2 hover:bg-panel2"
            >
              <div className="flex min-w-0 items-center gap-2 text-sm text-text">
                <div className="flex h-5 w-5 items-center justify-center rounded bg-accent/20">
                  {accessIcons[readScope] || accessIcons.fallback}
                </div>
                <span className="min-w-0 break-words">{readScope}</span>
              </div>
            </div>
          ))}
        </div>
      </div>

      <ConnectionHealthSection connector={connector} />

      {connector.id === 'salesforce' && isConnected && <SalesforceProductPicker />}

      {/* T2-S11-A Task T9: SQL Server scope declaration */}
      {/* Shown after SQL Server is connected — read scope selector */}
      {(connector.id === 'sql_server' || connector.id === 'sqlserver') && isConnected && (
        <div className="mt-4 border-t border-border pt-4">
          <SqlServerScopePicker />
        </div>
      )}

      {/* T2-S12-A: Oracle DB scope declaration */}
      {connector.id === 'oracle_db' && isConnected && (
        <div className="mt-4 border-t border-border pt-4">
          <OracleScopePicker />
        </div>
      )}

      {/* T2-S12-A: PostgreSQL scope declaration */}
      {connector.id === 'postgresql' && isConnected && (
        <div className="mt-4 border-t border-border pt-4">
          <PostgreSQLScopePicker />
        </div>
      )}

      <div className="mt-5">
        <Button
          variant="tertiary"
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
