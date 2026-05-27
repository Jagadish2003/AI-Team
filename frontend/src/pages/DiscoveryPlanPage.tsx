import React from 'react';
import {
  ArrowLeft,
  Clock3,
  FileText,
  Grid2X2,
  LineChart,
  MessageSquare,
  Play,
} from 'lucide-react';
import Button from '../components/common/Button';
import {
  QualityRow,
  QualityLevel,
  RecommendedAddition,
  SystemRole,
  FocusId,
  IndustryId,
} from '../types/stack_builder';
import { useSetupState } from '../components/stack_builder';

const SYSTEM_META: Record<string, { name: string }> = {
  sap: { name: 'SAP' },
  oracle_ebs: { name: 'Oracle EBS' },
  workday: { name: 'Workday' },
  dynamics365: { name: 'Dynamics 365' },
  salesforce: { name: 'Salesforce' },
  neospin: { name: 'Neospin' },
  vitech: { name: 'Vitech' },
  salesforce_pss: { name: 'Salesforce PSS' },
  salesforce_sc: { name: 'Salesforce SC' },
  salesforce_ncino: { name: 'nCino' },
  salesforce_fsc: { name: 'Salesforce FSC' },
  salesforce_rc: { name: 'Salesforce RC' },
  salesforce_hc: { name: 'Salesforce HC' },
  jira: { name: 'Jira' },
  servicenow: { name: 'ServiceNow' },
  azure_devops: { name: 'Azure DevOps' },
  linear: { name: 'Linear' },
  zendesk: { name: 'Zendesk' },
  slack: { name: 'Slack' },
  teams: { name: 'Microsoft Teams' },
  confluence: { name: 'Confluence' },
  sharepoint: { name: 'SharePoint' },
  notion: { name: 'Notion' },
  github: { name: 'GitHub' },
  gitlab: { name: 'GitLab' },
  bitbucket: { name: 'Bitbucket' },
  azure_repos: { name: 'Azure Repos' },
  postgresql: { name: 'PostgreSQL' },
  sql_server: { name: 'SQL Server' },
  oracle_db: { name: 'Oracle DB' },
  databricks: { name: 'Databricks' },
  snowflake: { name: 'Snowflake' },
  dbt: { name: 'dbt' },
};

const FOCUS_LABELS: Record<string, string> = {
  member_customer_service: 'Member / customer service',
  core_operations: 'Core operations',
  approvals_compliance: 'Approvals / compliance',
  cross_system_handoffs: 'Cross-system handoffs',
  back_office_productivity: 'Back-office productivity',
  engineering_change: 'Engineering / change',
  enterprise_wide: 'Enterprise-wide discovery',
};

const INDUSTRY_LABELS: Record<string, string> = {
  financial_services: 'Financial services',
  public_sector: 'Public sector',
  logistics_supply_chain: 'Logistics & supply chain',
  retail_commerce: 'Retail & commerce',
  healthcare: 'Healthcare',
  energy_utilities: 'Energy & utilities',
  manufacturing: 'Manufacturing',
  technology: 'Technology',
};

const TEMPLATE_LABELS: Record<string, string> = {
  commercial_lending: 'Commercial lending',
  public_retirement: 'Public retirement',
  service_operations: 'Service operations',
  revenue_operations: 'Revenue operations',
};

function getSystemName(id: string) {
  return SYSTEM_META[id]?.name ?? id;
}

function calcQualityRows(
  selectedIds: string[],
  weightings: Record<string, { role: SystemRole; priority?: string; workflowFocus: string[] }>,
): QualityRow[] {
  const hasPrimary = selectedIds.some(id =>
    weightings[id]?.priority === 'primary',
  );

  const signalSources = selectedIds.filter(id =>
    weightings[id]?.role === 'operational_signal_source',
  );

  const hasDoc = selectedIds.some(id =>
    weightings[id]?.role === 'documentation_system',
  );

  const hasCompliance = selectedIds.some(id =>
    weightings[id]?.workflowFocus?.includes('compliance_risk') ||
    weightings[id]?.workflowFocus?.includes('approvals'),
  );

  const hasEngineering = selectedIds.some(id =>
    weightings[id]?.role === 'engineering_change_system',
  );

  const signalLevel = (n: number): QualityLevel =>
    n >= 2 ? 'strong' : n === 1 ? 'moderate' : 'limited';

  return [
    {
      label: 'Workflow bottleneck detection',
      level: hasPrimary ? 'strong' : 'limited',
      descriptor: hasPrimary
        ? 'Strong - primary source priority confirmed'
        : 'Limited - no primary source selected',
    },
    {
      label: 'Cross-system corroboration',
      level: signalLevel(signalSources.length),
      descriptor: signalSources.length >= 2
        ? `Strong - ${signalSources.length} operational signal sources`
        : signalSources.length === 1
          ? 'Moderate - 1 operational signal source'
          : 'Limited - no operational signal sources selected',
    },
    {
      label: 'Compliance-sensitive workflows',
      level: hasCompliance ? 'strong' : 'limited',
      descriptor: hasCompliance
        ? 'Strong - compliance / risk focus confirmed'
        : 'Limited - no compliance focus configured',
    },
    {
      label: 'Documentation-driven interpretation',
      level: hasDoc ? 'moderate' : 'limited',
      descriptor: hasDoc
        ? 'Moderate - one documentation source'
        : 'Limited - no documentation system selected',
    },
    {
      label: 'Engineering / change signals',
      level: hasEngineering ? 'moderate' : 'limited',
      descriptor: hasEngineering
        ? 'Moderate - engineering system configured'
        : 'Limited - no code or engineering system selected',
    },
  ];
}

function calcRecommendedAdditions(
  selectedIds: string[],
  focusId: FocusId | null,
  industryId: IndustryId | null,
): RecommendedAddition[] {
  const additions: RecommendedAddition[] = [];

  const hasSlack = selectedIds.includes('slack') || selectedIds.includes('teams');
  const hasDoc = selectedIds.some(id => ['confluence', 'sharepoint', 'notion'].includes(id));
  const hasSignal = selectedIds.some(id => ['jira', 'servicenow', 'zendesk', 'linear', 'azure_devops'].includes(id));

  if (!hasSlack) {
    const reason = focusId === 'member_customer_service'
      ? 'Add communication signals for service escalation threads, team coordination delays, and cross-team handoff friction.'
      : focusId === 'cross_system_handoffs'
        ? 'Add communication signals for escalation threads and coordination delays that indicate handoff friction.'
        : 'Add communication signals from escalation threads, team coordination delays, and operational discussions.';
    additions.push({ systemId: 'slack', systemName: 'Slack', reason });
  }

  if (!hasDoc) {
    const reason = industryId === 'public_sector'
      ? 'Add policy documents, approval workflows, and regulatory reference materials common in public sector operating environments.'
      : focusId === 'approvals_compliance'
        ? 'Add policy documents and approval workflow documentation to strengthen compliance evidence.'
        : 'Add knowledge base articles, SOPs, and workflow documentation to strengthen process interpretation.';
    additions.push({ systemId: 'sharepoint', systemName: 'SharePoint', reason });
  }

  if (!hasSignal) {
    additions.push({
      systemId: 'jira',
      systemName: 'Jira',
      reason: 'Add ticket backlogs, work queue patterns, and process friction visible in issue tracking data.',
    });
  }

  return additions.slice(0, 3);
}

function QualityDot({ level }: { level: QualityLevel }) {
  const classes: Record<QualityLevel, string> = {
    strong: 'bg-emerald-500',
    moderate: 'bg-amber-400',
    limited: 'bg-slate-300',
  };

  return (
    <div
      className={`mt-1 h-2 w-2 flex-shrink-0 rounded-full ${classes[level]}`}
      aria-label={level}
      role="img"
    />
  );
}

type ChipVariant = 'primary' | 'signal';

function SystemChip({ name, variant }: { name: string; variant: ChipVariant }) {
  const classes: Record<ChipVariant, string> = {
    primary: 'border-accent bg-accent/15 text-blue-100',
    signal: 'border-border bg-bg/20 text-muted',
  };

  return (
    <span
      role="listitem"
      className={`inline-flex items-center gap-1.5 rounded-full border px-3 py-1 text-xs font-medium ${classes[variant]}`}
    >
      <span
        className={`h-1.5 w-1.5 flex-shrink-0 rounded-full ${
          variant === 'primary' ? 'bg-accent' : 'bg-muted'
        }`}
        aria-hidden="true"
      />
      {name}
    </span>
  );
}

function AdditionIcon({ systemId }: { systemId: string }) {
  const Icon = ['slack', 'teams'].includes(systemId) ? MessageSquare : FileText;
  return <Icon size={16} className="mt-0.5 flex-shrink-0 text-accent" aria-hidden="true" />;
}

function SummaryRow({
  label,
  value,
  valueClass = 'text-text',
}: {
  label: string;
  value: string;
  valueClass?: string;
}) {
  return (
    <div className="flex items-baseline justify-between gap-4 border-b border-border/70 py-2 last:border-0">
      <span className="text-xs text-muted">{label}</span>
      <span className={`text-right text-xs font-medium ${valueClass}`}>{value}</span>
    </div>
  );
}

interface Props {
  setupState: ReturnType<typeof useSetupState>;
  onLaunch: () => void;
}

export default function DiscoveryPlanPage({ setupState, onLaunch }: Props) {
  const { state, confidence } = setupState;

  const qualityRows = calcQualityRows(
    state.selectedSystemIds,
    state.weightings,
  );

  const recommendations = calcRecommendedAdditions(
    state.selectedSystemIds,
    state.focusId,
    state.industryId,
  );

  const primaryIds = state.selectedSystemIds.filter(id =>
    state.weightings[id]?.priority === 'primary',
  );

  const signalIds = state.selectedSystemIds.filter(id => !primaryIds.includes(id));

  const operationalSources = state.selectedSystemIds.filter(id =>
    state.weightings[id]?.role === 'operational_signal_source',
  );

  const docSystems = state.selectedSystemIds.filter(id =>
    state.weightings[id]?.role === 'documentation_system',
  );

  const confidenceLabel = confidence.level.charAt(0).toUpperCase() + confidence.level.slice(1);

  return (
    <div className="space-y-5">
      <div className="grid gap-4 xl:grid-cols-2">
        <section className="rounded-xl border border-border bg-panel p-5 shadow-sm">
          <div className="mb-3 flex items-center gap-2">
            <FileText size={16} className="text-accent" aria-hidden="true" />
            <h2 className="text-sm font-semibold text-text">Configuration summary</h2>
          </div>
          <div>
            <SummaryRow
              label="Discovery focus"
              value={state.focusId ? FOCUS_LABELS[state.focusId] : '-'}
              valueClass="text-blue-100"
            />
            <SummaryRow
              label="Industry"
              value={state.industryId ? INDUSTRY_LABELS[state.industryId] : '-'}
            />
            <SummaryRow
              label="Template"
              value={state.templateId ? TEMPLATE_LABELS[state.templateId] : '-'}
            />
            <SummaryRow
              label="Total systems"
              value={String(state.selectedSystemIds.length)}
            />
            <SummaryRow
              label="Primary systems"
              value={primaryIds.length > 0
                ? `${primaryIds.length} - ${primaryIds.map(getSystemName).join(', ')}`
                : '-'}
            />
            <SummaryRow
              label="Operational signal sources"
              value={operationalSources.length > 0
                ? `${operationalSources.length} - ${operationalSources.map(getSystemName).join(', ')}`
                : '-'}
            />
            <SummaryRow
              label="Documentation systems"
              value={docSystems.length > 0
                ? `${docSystems.length} - ${docSystems.map(getSystemName).join(', ')}`
                : '-'}
            />
          </div>
        </section>

        <section className="rounded-xl border border-border bg-panel p-5 shadow-sm">
          <div className="mb-3 flex items-center gap-2">
            <LineChart size={16} className="text-accent" aria-hidden="true" />
            <h2 className="text-sm font-semibold text-text">Expected discovery quality</h2>
          </div>
          <div className="space-y-3">
            {qualityRows.map(row => (
              <div key={row.label} className="flex items-start gap-2">
                <QualityDot level={row.level} />
                <div>
                  <div className="mb-0.5 text-xs font-medium leading-tight text-text">
                    {row.label}
                  </div>
                  <div className="text-xs leading-relaxed text-muted">
                    {row.descriptor}
                  </div>
                </div>
              </div>
            ))}
          </div>
        </section>
      </div>

      <section className="rounded-xl border border-border bg-panel p-5 shadow-sm">
        <div className="mb-3 flex items-center gap-2">
          <Grid2X2 size={16} className="text-accent" aria-hidden="true" />
          <h2 className="text-sm font-semibold text-text">Selected systems</h2>
        </div>

        <div role="list" className="mb-3 flex flex-wrap gap-2">
          {primaryIds.map(id => (
            <SystemChip key={id} name={getSystemName(id)} variant="primary" />
          ))}
          {signalIds.map(id => (
            <SystemChip key={id} name={getSystemName(id)} variant="signal" />
          ))}
        </div>

        <div className="flex flex-wrap items-center gap-4">
          <div className="flex items-center gap-1.5">
            <span className="h-2 w-2 flex-shrink-0 rounded-full bg-accent" aria-hidden="true" />
            <span className="text-xs text-muted">Primary priority</span>
          </div>
          <div className="flex items-center gap-1.5">
            <span className="h-2 w-2 flex-shrink-0 rounded-full bg-muted" aria-hidden="true" />
            <span className="text-xs text-muted">Operational signal source / documentation</span>
          </div>
        </div>
      </section>

      {/* ENG-SB-1 Sprint 9 (0-pt sub-task): Recommended additions section removed.
           Screen 2 (YourSystemsPage) now shows missing category prompts.
           Screen 4 is summary and launch only. */}

      <section className="rounded-xl border border-accent/30 bg-accent/10 p-5 shadow-sm">
        <div className="flex flex-col gap-4 lg:flex-row lg:items-center lg:justify-between">
          <div>
            <h2 className="text-sm font-semibold text-text">Ready to discover</h2>
            <p className="mt-1 text-sm leading-relaxed text-muted">
              {state.selectedSystemIds.length} system{state.selectedSystemIds.length !== 1 ? 's' : ''}
              {state.industryId ? ` / ${INDUSTRY_LABELS[state.industryId]}` : ''}
              {state.focusId ? ` / ${FOCUS_LABELS[state.focusId]} focus` : ''}
              {' / '}Confidence: {confidenceLabel}
            </p>
          </div>

          <Button
            variant="tertiary"
            onClick={onLaunch}
            ariaLabel={`Start discovery - confidence ${confidence.level}`}
            className="gap-2"
          >
            <Play size={16} fill="currentColor" strokeWidth={2.2} aria-hidden="true" />
            Start discovery
          </Button>
        </div>
      </section>

      <div className="rounded-xl border border-border bg-panel p-4 shadow-sm">
        <Button variant="tertiary" onClick={() => setupState.goTo(3)} className="gap-2">
          <ArrowLeft size={16} strokeWidth={2.2} aria-hidden="true" />
          Back
        </Button>
      </div>
    </div>
  );
}
