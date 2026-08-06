import React from 'react';
import { Connector, OutboundSetupRequest } from '../../types/connector';
import Badge from '../common/Badge';
import Button from '../common/Button';
import { accessIcons } from './AccessIcons';
import { useToast } from '../common/Toast';
import { useAuthOptional } from '../../context/AuthContext';
import { isViewerRole } from '../../utils/roles';
import { ExternalLink, CheckCircle2 } from 'lucide-react';
import SalesforceProductPicker from './SalesforceProductPicker';
import SlackChannelPicker from './SlackChannelPicker';
import TeamsChannelPicker from './TeamsChannelPicker';
import JiraProjectPicker from './JiraProjectPicker';
import ConfluenceSpacePicker from './ConfluenceSpacePicker';
import SharePointSitePicker from './SharePointSitePicker';
import GitHubRepoPicker from './GitHubRepoPicker';
import SqlServerScopePicker from './SqlServerScopePicker';
import OracleScopePicker from './OracleScopePicker';
import PostgreSQLScopePicker from './PostgreSQLScopePicker';
import StaticCredentialManager from './StaticCredentialManager';
import OutboundAuthSetup from './OutboundAuthSetup';
import { isStaticCredentialConnector } from './staticCredentialConnectors';
import MultiScopeConnectorManager from './MultiScopeConnectorManager';
import { isMultiScopeConnector } from './multiScopeConnectors';
import { useNetworkProfileOptional } from '../../context/NetworkProfileContext';

// T41-7: Connection Health - configured read scope for this connector.
// Shows what AgentIQ is configured to read from this source.
// Derived deterministically from connector.id (CONNECTION_HEALTH_LABELS map).
// IMPORTANT: reflects configured read scope, NOT proven last-sync results.
// Sprint 6 will wire this to real sync telemetry (Connection Health v2).

const CONNECTION_HEALTH_LABELS: Record<string, string[]> = {
  servicenow: [
    'Read Incident records',
    'Read operational signals',
    'Read SLA definitions',
  ],
  jira_confluence: [
    'Read Issue records',
    'Read operational signals',
    'Read Project configuration',
    'Read Space content',
  ],
  jira: [
    'Read Issue records',
    'Read operational signals',
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

// R18-C0 P1: the Salesforce read scope reflects the connected org's ACTUAL
// declared cloud products (GET/PATCH /api/connectors/salesforce/products →
// connector.products), never a hardcoded catalog category. Standard objects are
// always readable; each declared product AgentIQ ingests adds its own objects.
// Products AgentIQ does not specifically ingest (FSC / Revenue / Health / PSS)
// simply read the standard base scope — no invented or POC object names.
const SALESFORCE_BASE_READ_SCOPE = [
  'Read Account records',
  'Read Contact records',
  'Read Opportunity records',
  'Read Case records',
];

const SALESFORCE_PRODUCT_READ_SCOPE: Record<string, string[]> = {
  // Service Cloud
  salesforce_sc: [
    'Read Flow metadata',
    'Read Approval history',
    'Read OpportunityLineItem records',
  ],
  // nCino commercial lending (managed package objects)
  salesforce_ncino: [
    'Read LLC_BI__Loan__c records',
    'Read LLC_BI__Covenant2__c records',
    'Read LLC_BI__Checklist__c records',
    'Read LLC_BI__Spread_Statement_Period__c records',
    'Read ProcessInstance records',
  ],
};

// Build the Salesforce connection read scope from the org's declared products.
// With no declaration we show only the standard base objects — never nCino
// scope by default. Deterministic order: base first, then products in the order
// declared, de-duplicated.
function salesforceReadScope(products: string[] | undefined): string[] {
  const scope = [...SALESFORCE_BASE_READ_SCOPE];
  for (const product of products ?? []) {
    for (const label of SALESFORCE_PRODUCT_READ_SCOPE[product] ?? []) {
      if (!scope.includes(label)) scope.push(label);
    }
  }
  return scope;
}

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

  const items =
    connector.id === 'salesforce'
      ? salesforceReadScope(connector.products)
      : CONNECTION_HEALTH_LABELS[connector.id] ??
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
        Configured read scope for this connector. Actual sync results coming soon.
      </p>
    </div>
  );
}

export default function ConnectorDetailPanel({
  connector,
  onConfigure,
  outboundSetupRequest = null,
}: {
  connector: Connector | null;
  onConfigure: () => void;
  // R18-A3 follow-up: a one-shot request (from the tile's "Set up outbound
  // access" button) to auto-open this connector's setup modal. Forwarded to the
  // static / outbound setup managers, which open their own modal when it matches.
  outboundSetupRequest?: OutboundSetupRequest | null;
}) {
  const { push } = useToast();
  // Configure & Sync / Re-sync triggers a write (analyst+). Viewers get a
  // read-only panel — this action is disabled for them.
  const auth = useAuthOptional();
  const isViewer = isViewerRole(auth?.user?.role);
  // R18-A3: the static-credential vault flow is a NO-PUBLIC-INBOUND feature. Its
  // only write entry point is the "Set up outbound access" button, which
  // OutboundAuthSetup renders ONLY when noPublicInbound. In the standard profile
  // these connectors authenticate via the normal Connect (OAuth) flow / env, so
  // the credentials card would just point at a button that isn't there.
  const { noPublicInbound } = useNetworkProfileOptional();

  if (!connector) {
    return (
      <div className="rounded-xl border border-border bg-panel p-4 text-sm text-muted">
        Select a connector to view details.
      </div>
    );
  }

  const isConnected = connector.status === 'connected';
  const isConfigured = connector.configured;
  const isUnavailable = connector.roadmap === true || connector.status === 'coming_soon';
  const displayStatus = isUnavailable ? 'not_configured' : connector.status;
  const viewerOnlyScope = isViewerOnlyScopeUser();

  // MSP-B13 (AT-744): AWS/Azure Event connectors onboard through the shared
  // multi-scope card (one connection, many accounts/subscriptions). It owns the
  // whole onboarding surface (credentials, test, scope panel + health), so the
  // panel renders a dedicated body for it instead of the single-connection
  // Access-as / Configure layout below.
  if (isMultiScopeConnector(connector.id)) {
    return (
      <div className="rounded-xl border border-border bg-panel p-5">
        <div className="flex items-start justify-between gap-2">
          <div className="min-w-0">
            <div className="break-words text-xl font-semibold leading-snug text-text">
              {connector.name} Integration
            </div>
            <div className="mt-1 break-words text-sm text-muted">{connector.category}</div>
          </div>
          <Badge status={displayStatus} />
        </div>
        <div className="mt-4 border-t border-border" />
        <div className="mt-4">
          <MultiScopeConnectorManager connector={connector} />
        </div>
      </div>
    );
  }

  return (
    <div className="rounded-xl border border-border bg-panel p-5">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="break-words text-xl font-semibold leading-snug text-text">
            {connector.name} Integration
          </div>
          <div className="mt-1 break-words text-sm text-muted">{connector.category}</div>
        </div>

        <Badge status={displayStatus} />
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
      {isStaticCredentialConnector(connector.id) && noPublicInbound && (
        <div className="mt-4">
          <StaticCredentialManager connector={connector} outboundSetupRequest={outboundSetupRequest} />
        </div>
      )}

      {/* R18-A3 T5 (AT-558): outbound setup path — shown in a no-public-inbound
          deployment for connectors whose outbound-only mode is jwt_bearer or
          client_credentials (Salesforce, Teams/SharePoint, ServiceNow). This is
          where the customer is routed after the authorization-code Connect button
          is hidden on the tile (AC4). Renders null outside no_public_inbound. */}
      <OutboundAuthSetup connector={connector} outboundSetupRequest={outboundSetupRequest} />

      {/* Salesforce product declaration — rendered first in the panel so the
          workspace declaration is visible at the top of the right panel. */}
      {connector.id === 'salesforce' && isConnected && (
        <>
          <SalesforceProductPicker />
          <div className="mt-4 border-t border-border" />
        </>
      )}

      {/* R18-C0 P5: Slack channel selection — pick which channels AgentIQ reads.
          The picker carries the R18-A4 depth-phase consent notice inline. */}
      {connector.id === 'slack' && isConnected && (
        <>
          <SlackChannelPicker />
          <div className="mt-4 border-t border-border" />
        </>
      )}

      {/* Jira project selection — pick which project AgentIQ scopes discovery to
          (single-project; the Jira analogue of the Slack channel selection). */}
      {connector.id === 'jira' && isConnected && (
        <>
          <JiraProjectPicker />
          <div className="mt-4 border-t border-border" />
        </>
      )}

      {/* Teams channel selection — pick which granted channels AgentIQ reads
          (multi-select; the Teams analogue of the Slack channel selection). The
          picker carries the R18-A4 depth-phase consent notice inline. */}
      {connector.id === 'teams' && isConnected && (
        <>
          <TeamsChannelPicker />
          <div className="mt-4 border-t border-border" />
        </>
      )}

      {/* Confluence space selection — pick which granted spaces AgentIQ reads
          (multi-select; the Confluence analogue of the Slack channel selection). */}
      {connector.id === 'confluence' && isConnected && (
        <>
          <ConfluenceSpacePicker />
          <div className="mt-4 border-t border-border" />
        </>
      )}

      {/* SharePoint site selection — pick which granted sites AgentIQ reads
          (multi-select; the SharePoint analogue of the Slack channel selection). */}
      {connector.id === 'sharepoint' && isConnected && (
        <>
          <SharePointSitePicker />
          <div className="mt-4 border-t border-border" />
        </>
      )}

      {/* GitHub repository selection — pick which repositories AgentIQ reads
          (multi-select; the GitHub analogue of the Slack channel selection). */}
      {connector.id === 'github' && isConnected && (
        <>
          <GitHubRepoPicker />
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
          disabled={!isConnected || isUnavailable || isViewer}
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
