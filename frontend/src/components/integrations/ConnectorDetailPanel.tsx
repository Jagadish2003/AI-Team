import React from 'react';
import { Connector } from '../../types/connector';
import Badge from '../common/Badge';
import Button from '../common/Button';
import { accessIcons } from './AccessIcons';
import { useToast } from '../common/Toast';
import { useAuthOptional } from '../../context/AuthContext';
import { isViewerRole } from '../../utils/roles';
import { ExternalLink, CheckCircle2 } from 'lucide-react';
import SalesforceProductPicker from './SalesforceProductPicker';
import SqlServerScopePicker from './SqlServerScopePicker';
import OracleScopePicker from './OracleScopePicker';
import PostgreSQLScopePicker from './PostgreSQLScopePicker';
import StaticCredentialManager from './StaticCredentialManager';
import OutboundAuthSetup from './OutboundAuthSetup';
import { isStaticCredentialConnector } from './staticCredentialConnectors';

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

function isViewerOnlyScopeUser(): boolean {
  const role = (import.meta.env.VITE_DEV_JWT_ROLE as string | undefined)
    ?.trim()
    .toLowerCase();
  if (role) return role === 'viewer';

  const token =
    (import.meta.env.VITE_DEV_JWT as string | undefined) ??
    'dev-token-change-me';
  const viewerToken =
    (import.meta.env.VITE_VIEWER_JWT as string | undefined) ?? 'viewer-token';

  return token === viewerToken;
}

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
  outboundIntentId,
  onOutboundIntentHandled,
}: {
  connector: Connector | null;
  onConfigure: () => void;
  // R18-A3 T5 (AT-558): when this equals the shown connector's id, auto-open its
  // outbound setup (the JWT-bearer modal); onOutboundIntentHandled clears it.
  outboundIntentId?: string | null;
  onOutboundIntentHandled?: () => void;
}) {
  const { push } = useToast();
  // Configure & Sync / Re-sync triggers a write (analyst+). Viewers get a
  // read-only panel — this action is disabled for them.
  const auth = useAuthOptional();
  const isViewer = isViewerRole(auth?.user?.role);

  if (!connector) {
    return (
      <div className="rounded-xl border border-border bg-panel p-4 text-sm text-muted">
        Select a connector to view details.
      </div>
    );
  }

  const isConnected = connector.status === 'connected';
  const isConfigured = connector.configured;
  const viewerOnlyScope = isViewerOnlyScopeUser();

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
          onClick={() => push('Coming Soon')}
          className="inline-flex items-center gap-1 rounded-md border border-accent/20 bg-accent/5 px-2 py-1 text-xs font-medium text-accent transition-colors hover:border-accent/45 hover:bg-accent/10 focus:outline-none focus-visible:ring-1 focus-visible:ring-accent/40"
        >
          Learn More <ExternalLink size={14} />
        </button>
      </div>

      <div className="mt-4 border-t border-border" />

      {/* R17-D3 Addendum A (T12 / AC10): static-credential entry for connectors
          that authenticate with URL + username + token/password (Jira,
          ServiceNow, native DBs). Shown near the top because entering the
          credential is how these connectors get connected. Owner-gated inside. */}
      {isStaticCredentialConnector(connector.id) && (
        <div className="mt-4">
          <StaticCredentialManager connector={connector} />
        </div>
      )}

      {/* R18-A3 T5 (AT-558): outbound setup path — shown in a no-public-inbound
          deployment for connectors whose outbound-only mode is jwt_bearer or
          client_credentials (Salesforce, Teams/SharePoint, ServiceNow). This is
          where the customer is routed after the authorization-code Connect button
          is hidden on the tile (AC4). Renders null outside no_public_inbound. */}
      <OutboundAuthSetup
        connector={connector}
        autoOpen={outboundIntentId === connector.id}
        onAutoOpenHandled={onOutboundIntentHandled}
      />

      {/* Salesforce product declaration — rendered first in the panel so the
          workspace declaration is visible at the top of the right panel. */}
      {connector.id === 'salesforce' && isConnected && (
        <>
          <SalesforceProductPicker />
          <div className="mt-4 border-t border-border" />
        </>
      )}

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
          <OracleScopePicker viewerOnly={viewerOnlyScope} />
        </div>
      )}

      {/* T2-S12-A: PostgreSQL scope declaration */}
      {connector.id === 'postgresql' && isConnected && (
        <div className="mt-4 border-t border-border pt-4">
          <PostgreSQLScopePicker viewerOnly={viewerOnlyScope} />
        </div>
      )}

      <div className="mt-5">
        <Button
          variant="tertiary"
          className="w-full whitespace-nowrap"
          onClick={onConfigure}
          disabled={!isConnected || connector.status === 'coming_soon' || isViewer}
          title={
            isViewer
              ? 'Configuring a source requires an analyst or owner role.'
              : !isConnected
              ? 'Connect this source first'
              : undefined
          }
        >
          {isConfigured ? 'Re-sync' : 'Configure & Sync'}
        </Button>
      </div>
    </div>
  );
}
