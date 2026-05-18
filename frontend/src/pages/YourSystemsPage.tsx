import React from 'react';
import {
  ArrowLeft,
  Code2,
  Database,
  Info,
  ListChecks,
  MessageSquare,
  MoveRight,
} from 'lucide-react';
import Button from '../components/common/Button';
import {
  SystemCard as SystemCardType,
  ConnectionStatus,
  FocusId,
} from '../types/stack_builder';
import { SystemCard } from '../components/stack_builder';
import { useSetupState } from '../components/stack_builder';

const PRIMARY_PLATFORMS: SystemCardType[] = [
  { id: 'salesforce', name: 'Salesforce', category: 'CRM / industry', group: 'primary_platform', connectionStatus: 'not_configured', logoInitials: 'SF', logoColor: 'bg-sky-500', isSalesforce: true },
  { id: 'sap', name: 'SAP', category: 'ERP', group: 'primary_platform', connectionStatus: 'not_configured', logoInitials: 'SAP', logoColor: 'bg-blue-700' },
  { id: 'oracle_ebs', name: 'Oracle EBS', category: 'Finance / HR', group: 'primary_platform', connectionStatus: 'not_configured', logoInitials: 'ORC', logoColor: 'bg-red-700' },
  { id: 'workday', name: 'Workday', category: 'HR / finance', group: 'primary_platform', connectionStatus: 'not_configured', logoInitials: 'WD', logoColor: 'bg-yellow-600' },
  { id: 'dynamics365', name: 'Dynamics 365', category: 'ERP / CRM', group: 'primary_platform', connectionStatus: 'not_configured', logoInitials: 'D365', logoColor: 'bg-blue-600' },
];

const ADDITIONAL_PLATFORMS: SystemCardType[] = PRIMARY_PLATFORMS.filter(
  p => !p.isSalesforce,
);

const WORK_TRACKING: SystemCardType[] = [
  { id: 'jira', name: 'Jira', category: 'Issues / backlog', group: 'work_tracking', connectionStatus: 'not_configured', logoInitials: 'JR', logoColor: 'bg-blue-600' },
  { id: 'servicenow', name: 'ServiceNow', category: 'ITSM / operations', group: 'work_tracking', connectionStatus: 'not_configured', logoInitials: 'SN', logoColor: 'bg-green-700' },
  { id: 'azure_devops', name: 'Azure DevOps', category: 'ALM / CI/CD', group: 'work_tracking', connectionStatus: 'not_configured', logoInitials: 'ADO', logoColor: 'bg-blue-700' },
  { id: 'linear', name: 'Linear', category: 'Product / issues', group: 'work_tracking', connectionStatus: 'not_configured', logoInitials: 'LN', logoColor: 'bg-violet-600' },
  { id: 'zendesk', name: 'Zendesk', category: 'Support', group: 'work_tracking', connectionStatus: 'not_configured', logoInitials: 'ZD', logoColor: 'bg-green-600' },
];

const COMMS_KNOWLEDGE: SystemCardType[] = [
  { id: 'slack', name: 'Slack', category: 'Messaging', group: 'comms_knowledge', connectionStatus: 'not_configured', logoInitials: 'SL', logoColor: 'bg-purple-600' },
  { id: 'teams', name: 'Microsoft Teams', category: 'Comms / docs', group: 'comms_knowledge', connectionStatus: 'not_configured', logoInitials: 'MS', logoColor: 'bg-blue-700' },
  { id: 'confluence', name: 'Confluence', category: 'Docs / knowledge', group: 'comms_knowledge', connectionStatus: 'not_configured', logoInitials: 'CF', logoColor: 'bg-blue-500' },
  { id: 'sharepoint', name: 'SharePoint', category: 'Docs / intranet', group: 'comms_knowledge', connectionStatus: 'not_configured', logoInitials: 'SP', logoColor: 'bg-blue-600' },
  { id: 'notion', name: 'Notion', category: 'Docs / wiki', group: 'comms_knowledge', connectionStatus: 'not_configured', logoInitials: 'NO', logoColor: 'bg-slate-700' },
];

const CODE_ENGINEERING: SystemCardType[] = [
  { id: 'github', name: 'GitHub', category: 'Source control', group: 'code_engineering', connectionStatus: 'not_configured', logoInitials: 'GH', logoColor: 'bg-slate-800' },
  { id: 'gitlab', name: 'GitLab', category: 'DevOps', group: 'code_engineering', connectionStatus: 'not_configured', logoInitials: 'GL', logoColor: 'bg-orange-600' },
  { id: 'bitbucket', name: 'Bitbucket', category: 'Source control', group: 'code_engineering', connectionStatus: 'not_configured', logoInitials: 'BB', logoColor: 'bg-blue-600' },
  { id: 'azure_repos', name: 'Azure Repos', category: 'Source control', group: 'code_engineering', connectionStatus: 'not_configured', logoInitials: 'AR', logoColor: 'bg-blue-700' },
];

const DATA_INFRASTRUCTURE: SystemCardType[] = [
  { id: 'postgresql', name: 'PostgreSQL', category: 'Database', group: 'data_infrastructure', connectionStatus: 'not_configured', logoInitials: 'PG', logoColor: 'bg-blue-700' },
  { id: 'sql_server', name: 'SQL Server', category: 'Database', group: 'data_infrastructure', connectionStatus: 'not_configured', logoInitials: 'SQL', logoColor: 'bg-red-700' },
  { id: 'oracle_db', name: 'Oracle DB', category: 'Database', group: 'data_infrastructure', connectionStatus: 'not_configured', logoInitials: 'ORC', logoColor: 'bg-red-600' },
  { id: 'databricks', name: 'Databricks', category: 'Data platform', group: 'data_infrastructure', connectionStatus: 'not_configured', logoInitials: 'DB', logoColor: 'bg-orange-500' },
  { id: 'snowflake', name: 'Snowflake', category: 'Data warehouse', group: 'data_infrastructure', connectionStatus: 'not_configured', logoInitials: 'SF', logoColor: 'bg-sky-500' },
  { id: 'dbt', name: 'dbt', category: 'Transforms', group: 'data_infrastructure', connectionStatus: 'not_configured', logoInitials: 'dbt', logoColor: 'bg-orange-600' },
];

function getRecommendationReason(
  systemId: string,
  focusId: FocusId | null,
): string | undefined {
  if (!focusId) return undefined;

  const map: Partial<Record<string, Partial<Record<FocusId, string>>>> = {
    jira: {
      member_customer_service: 'Recommended for workflow signals',
      core_operations: 'Recommended for workflow signals',
      approvals_compliance: 'Recommended for compliance signals',
      cross_system_handoffs: 'Recommended for handoff signals',
      back_office_productivity: 'Recommended for backlog signals',
      engineering_change: 'Recommended for change signals',
      enterprise_wide: 'Recommended for workflow signals',
    },
    servicenow: {
      member_customer_service: 'Recommended for service signals',
      core_operations: 'Recommended for operational signals',
      approvals_compliance: 'Recommended for compliance signals',
      cross_system_handoffs: 'Recommended for incident signals',
      back_office_productivity: 'Recommended for process signals',
      engineering_change: 'Recommended for change signals',
      enterprise_wide: 'Recommended for compliance signals',
    },
    confluence: {
      member_customer_service: 'Recommended for process docs',
      approvals_compliance: 'Recommended for policy docs',
      back_office_productivity: 'Recommended for process docs',
      enterprise_wide: 'Recommended for process docs',
    },
    slack: {
      member_customer_service: 'Recommended for comms signals',
      cross_system_handoffs: 'Recommended for comms signals',
      enterprise_wide: 'Recommended for comms signals',
    },
    sharepoint: {
      approvals_compliance: 'Recommended for policy docs',
      back_office_productivity: 'Recommended for process docs',
    },
  };

  return map[systemId]?.[focusId];
}

function GroupLabel({ label }: { label: string }) {
  return (
    <div className="text-xs font-semibold uppercase tracking-widest text-muted">
      {label}
    </div>
  );
}

function SubGroupHeader({
  icon,
  label,
  count,
}: {
  icon: React.ReactNode;
  label: string;
  count: number;
}) {
  return (
    <div className="mb-3 flex items-center gap-2">
      <span className="text-accent" aria-hidden="true">{icon}</span>
      <span className="text-sm font-semibold text-text">{label}</span>
      {count > 0 && (
        <span className="rounded-full border border-accent/30 bg-accent/10 px-2 py-0.5 text-xs font-medium text-blue-100">
          {count} selected
        </span>
      )}
    </div>
  );
}

function ConnectionStatusLegendCorrected() {
  const items = [
    { color: 'bg-emerald-500', label: 'Connected' },
    { color: 'bg-amber-400', label: 'Credentials needed' },
    { color: 'bg-slate-300', label: 'Not yet configured' },
  ];

  return (
    <div className="flex flex-wrap items-center gap-x-5 gap-y-2">
      {items.map(item => (
        <div key={item.label} className="flex items-center gap-1.5">
          <div className={`h-2 w-2 rounded-full ${item.color}`} aria-hidden="true" />
          <span className="text-xs text-muted">{item.label}</span>
        </div>
      ))}
      <span className="text-xs text-muted">
        Authentication for unconnected systems is managed in <span className="text-accent">Integration Hub</span>.
      </span>
    </div>
  );
}

function SystemGrid({
  label,
  systems,
  connectionStatuses,
  selectedIds,
  onToggle,
  focusId,
}: {
  label: string;
  systems: SystemCardType[];
  connectionStatuses: ConnectionStatusMap;
  selectedIds: string[];
  onToggle: (id: string) => void;
  focusId?: FocusId | null;
}) {
  return (
    <div
      role="group"
      aria-label={label}
      className="grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-3 2xl:grid-cols-4"
    >
      {systems.map(system => (
        <SystemCard
          key={system.id}
          system={withConnectionStatus(system, connectionStatuses)}
          selected={selectedIds.includes(system.id)}
          recommendationReason={getRecommendationReason(system.id, focusId ?? null)}
          onToggle={onToggle}
        />
      ))}
    </div>
  );
}

type ConnectionStatusMap = Partial<Record<string, ConnectionStatus>>;

function withConnectionStatus(
  system: SystemCardType,
  connectionStatuses: ConnectionStatusMap,
): SystemCardType {
  return {
    ...system,
    connectionStatus: connectionStatuses[system.id] ?? system.connectionStatus,
  };
}

interface Props {
  setupState: ReturnType<typeof useSetupState>;
  connectionStatuses: ConnectionStatusMap;
}

export default function YourSystemsPage({
  setupState,
  connectionStatuses,
}: Props) {
  const {
    state,
    toggleSystem,
    goTo,
    canProceedFromStep2,
  } = setupState;

  console.log(`state:`, state);

  const selectedPrimaryId = PRIMARY_PLATFORMS.find(p =>
    state.selectedSystemIds.includes(p.id)
  )?.id ?? null;

  function handlePrimarySelect(systemId: string) {
    if (selectedPrimaryId === systemId) {
      toggleSystem(systemId);
      return;
    }

    if (selectedPrimaryId) toggleSystem(selectedPrimaryId);
    toggleSystem(systemId);
  }

  const availableAdditional = ADDITIONAL_PLATFORMS.filter(
    p => p.id !== selectedPrimaryId && p.id !== 'salesforce',
  );

  const countInGroup = (systems: SystemCardType[]) =>
    systems.filter(s => state.selectedSystemIds.includes(s.id)).length;

  const totalSelected = state.selectedSystemIds.length;

  return (
    <div className="space-y-5">
      <GroupLabel label="Group A - Primary business platforms" />

      <section className="rounded-xl border border-border bg-panel p-5 shadow-sm">
        <h2 className="text-lg font-semibold text-text">Where your core operation runs</h2>
        <p className="mt-1 text-sm leading-relaxed text-muted">
          Select the platform where your primary business workflows live.
        </p>

        <div
          role="radiogroup"
          aria-label="Primary business platform"
          className="mt-4 grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-3 2xl:grid-cols-4"
        >
          {PRIMARY_PLATFORMS.map(system => (
            <SystemCard
              key={system.id}
              system={withConnectionStatus(system, connectionStatuses)}
              selected={state.selectedSystemIds.includes(system.id)}
              recommendationReason={getRecommendationReason(system.id, state.focusId)}
              onToggle={handlePrimarySelect}
              selectionRole="radio"
            />
          ))}
        </div>

      </section>

      <section className="rounded-xl border border-border bg-panel p-5 shadow-sm">
        <div className="mb-1 flex flex-wrap items-center gap-2">
          <h2 className="text-sm font-semibold text-text">
            Other platforms involved in the same workflows
          </h2>
          <span className="text-xs text-muted">Optional</span>
        </div>
        <p className="mb-4 text-xs leading-relaxed text-muted">
          Add any other operational platforms materially involved in the workflows you
          want to analyze.
        </p>

        <SystemGrid
          label="Additional platforms"
          systems={availableAdditional}
          connectionStatuses={connectionStatuses}
          selectedIds={state.selectedSystemIds}
          onToggle={toggleSystem}
        />
      </section>

      <GroupLabel label="Group B - Operational systems" />

      <section className="rounded-xl border border-border bg-panel p-5 shadow-sm">
        <SubGroupHeader
          icon={<ListChecks size={16} />}
          label="Work tracking & operations"
          count={countInGroup(WORK_TRACKING)}
        />
        <SystemGrid
          label="Work tracking and operations systems"
          systems={WORK_TRACKING}
          connectionStatuses={connectionStatuses}
          selectedIds={state.selectedSystemIds}
          onToggle={toggleSystem}
          focusId={state.focusId}
        />
      </section>

      <section className="rounded-xl border border-border bg-panel p-5 shadow-sm">
        <SubGroupHeader
          icon={<MessageSquare size={16} />}
          label="Communications & knowledge"
          count={countInGroup(COMMS_KNOWLEDGE)}
        />
        <SystemGrid
          label="Communications and knowledge systems"
          systems={COMMS_KNOWLEDGE}
          connectionStatuses={connectionStatuses}
          selectedIds={state.selectedSystemIds}
          onToggle={toggleSystem}
          focusId={state.focusId}
        />
      </section>

      <GroupLabel label="Group C - Data & engineering sources" />

      <section className="rounded-xl border border-border bg-panel p-5 shadow-sm">
        <SubGroupHeader
          icon={<Code2 size={16} />}
          label="Code & engineering"
          count={countInGroup(CODE_ENGINEERING)}
        />
        <SystemGrid
          label="Code and engineering systems"
          systems={CODE_ENGINEERING}
          connectionStatuses={connectionStatuses}
          selectedIds={state.selectedSystemIds}
          onToggle={toggleSystem}
        />
      </section>

      <section className="rounded-xl border border-border bg-panel p-5 shadow-sm">
        <SubGroupHeader
          icon={<Database size={16} />}
          label="Data & infrastructure"
          count={countInGroup(DATA_INFRASTRUCTURE)}
        />
        <SystemGrid
          label="Data and infrastructure systems"
          systems={DATA_INFRASTRUCTURE}
          connectionStatuses={connectionStatuses}
          selectedIds={state.selectedSystemIds}
          onToggle={toggleSystem}
        />
      </section>

      <section className="rounded-xl border border-border bg-panel p-4 shadow-sm">
        <ConnectionStatusLegendCorrected />
      </section>

      <div
        role="alert"
        aria-live="polite"
        className="rounded-xl border border-accent/30 bg-accent/10 px-4 py-3"
      >
        <div className="flex items-start gap-2">
          <Info size={16} className="mt-0.5 shrink-0 text-accent" aria-hidden="true" />
          <p className="text-sm leading-relaxed text-blue-100">
            At least one primary business platform must be selected before continuing.
            Authentication for unconnected systems is managed separately in Integration Hub.
          </p>
        </div>
      </div>

      <div className="rounded-xl border border-border bg-panel p-4 shadow-sm">
        <div className="flex flex-col gap-4 md:flex-row md:items-center md:justify-between">
          <Button variant="secondary" onClick={() => goTo(1)} className="gap-2">
            <ArrowLeft size={16} strokeWidth={2.2} aria-hidden="true" />
            Back
          </Button>

          <span
            className="text-sm text-muted"
            aria-live="polite"
            aria-label={`${totalSelected} systems selected`}
          >
            {totalSelected} system{totalSelected !== 1 ? 's' : ''} selected
          </span>

          <Button
            onClick={() => canProceedFromStep2 && goTo(3)}
            disabled={!canProceedFromStep2}
            className="gap-2"
          >
            Continue to source weighting
            <MoveRight size={16} strokeWidth={2.2} aria-hidden="true" />
          </Button>
        </div>
      </div>
    </div>
  );
}
