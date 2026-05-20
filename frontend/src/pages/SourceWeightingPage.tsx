import React from 'react';
import { ArrowLeft, Info, MoveRight } from 'lucide-react';
import Button from '../components/common/Button';
import { SystemWeightingCard } from '../components/stack_builder';
import { useSetupState } from '../components/stack_builder';

const SYSTEM_META: Record<string, { name: string; logoInitials: string; logoColor: string }> = {
  sap: { name: 'SAP', logoInitials: 'SAP', logoColor: 'bg-blue-700' },
  oracle_ebs: { name: 'Oracle EBS', logoInitials: 'ORC', logoColor: 'bg-red-700' },
  workday: { name: 'Workday', logoInitials: 'WD', logoColor: 'bg-yellow-600' },
  dynamics365: { name: 'Dynamics 365', logoInitials: 'D365', logoColor: 'bg-blue-600' },
  salesforce: { name: 'Salesforce', logoInitials: 'SF', logoColor: 'bg-sky-500' },
  neospin: { name: 'Neospin', logoInitials: 'NS', logoColor: 'bg-teal-700' },
  vitech: { name: 'Vitech', logoInitials: 'VT', logoColor: 'bg-green-700' },

  salesforce_pss: { name: 'Salesforce - Public Sector Solutions', logoInitials: 'SF', logoColor: 'bg-sky-500' },
  salesforce_sc: { name: 'Salesforce - Service Cloud', logoInitials: 'SF', logoColor: 'bg-sky-500' },
  salesforce_ncino: { name: 'Salesforce - nCino', logoInitials: 'SF', logoColor: 'bg-sky-500' },
  salesforce_fsc: { name: 'Salesforce - Financial Services Cloud', logoInitials: 'SF', logoColor: 'bg-sky-500' },
  salesforce_rc: { name: 'Salesforce - Revenue Cloud', logoInitials: 'SF', logoColor: 'bg-sky-500' },
  salesforce_hc: { name: 'Salesforce - Health Cloud', logoInitials: 'SF', logoColor: 'bg-sky-500' },

  jira: { name: 'Jira', logoInitials: 'JR', logoColor: 'bg-blue-600' },
  servicenow: { name: 'ServiceNow', logoInitials: 'SN', logoColor: 'bg-green-700' },
  azure_devops: { name: 'Azure DevOps', logoInitials: 'ADO', logoColor: 'bg-blue-700' },
  linear: { name: 'Linear', logoInitials: 'LN', logoColor: 'bg-violet-600' },
  zendesk: { name: 'Zendesk', logoInitials: 'ZD', logoColor: 'bg-green-600' },

  slack: { name: 'Slack', logoInitials: 'SL', logoColor: 'bg-purple-600' },
  teams: { name: 'Microsoft Teams', logoInitials: 'MS', logoColor: 'bg-blue-700' },
  confluence: { name: 'Confluence', logoInitials: 'CF', logoColor: 'bg-blue-500' },
  sharepoint: { name: 'SharePoint', logoInitials: 'SP', logoColor: 'bg-blue-600' },
  notion: { name: 'Notion', logoInitials: 'NO', logoColor: 'bg-slate-700' },

  github: { name: 'GitHub', logoInitials: 'GH', logoColor: 'bg-slate-800' },
  gitlab: { name: 'GitLab', logoInitials: 'GL', logoColor: 'bg-orange-600' },
  bitbucket: { name: 'Bitbucket', logoInitials: 'BB', logoColor: 'bg-blue-600' },
  azure_repos: { name: 'Azure Repos', logoInitials: 'AR', logoColor: 'bg-blue-700' },

  postgresql: { name: 'PostgreSQL', logoInitials: 'PG', logoColor: 'bg-blue-700' },
  sql_server: { name: 'SQL Server', logoInitials: 'SQL', logoColor: 'bg-red-700' },
  oracle_db: { name: 'Oracle DB', logoInitials: 'ORC', logoColor: 'bg-red-600' },
  databricks: { name: 'Databricks', logoInitials: 'DB', logoColor: 'bg-orange-500' },
  snowflake: { name: 'Snowflake', logoInitials: 'SF', logoColor: 'bg-sky-500' },
  dbt: { name: 'dbt', logoInitials: 'dbt', logoColor: 'bg-orange-600' },
};

function getSystemMeta(id: string) {
  return SYSTEM_META[id] ?? {
    name: id,
    logoInitials: id.slice(0, 2).toUpperCase(),
    logoColor: 'bg-slate-600',
  };
}

interface Props {
  setupState: ReturnType<typeof useSetupState>;
}

export default function SourceWeightingPage({ setupState }: Props) {
  const {
    state,
    updateWeighting,
    showEngineeringRole,
    goTo,
  } = setupState;

  return (
    <div className="space-y-5">
      <section className="rounded-xl border border-border bg-panel p-5 shadow-sm">
        <div className="mb-4 flex flex-col gap-1">
          <h2 className="text-lg font-semibold text-text">Source weighting</h2>
          <p className="text-sm leading-relaxed text-muted">
            Confirm how each system contributes to discovery so AgentIQ can weight
            evidence correctly. Smart defaults are already filled.
          </p>
        </div>

        {state.selectedSystemIds.length === 0 ? (
          <div className="rounded-lg border border-border bg-bg/20 p-4 text-sm text-muted">
            No systems selected yet. Go back to choose at least one source.
          </div>
        ) : (
          <div className="space-y-3">
            {state.selectedSystemIds.map((id, index) => {
              const meta = getSystemMeta(id);
              const weighting = state.weightings[id];

              if (!weighting) return null;

              return (
                <SystemWeightingCard
                  key={id}
                  id={`weighting-card-${id}`}
                  systemName={meta.name}
                  logoInitials={meta.logoInitials}
                  logoColor={meta.logoColor}
                  weighting={weighting}
                  showEngineeringRole={showEngineeringRole}
                  onChange={updateWeighting}
                  onConfirm={() => {
                    const nextUnconfirmed = state.selectedSystemIds.slice(index + 1).find(
                      nextId => !state.weightings[nextId]?.confirmed,
                    );
                    if (nextUnconfirmed) {
                      const el = document.getElementById(`weighting-card-${nextUnconfirmed}`);
                      el?.scrollIntoView({ behavior: 'smooth', block: 'start' });
                    }
                  }}
                />
              );
            })}
          </div>
        )}
      </section>

      {showEngineeringRole && (
        <div
          role="note"
          className="rounded-xl border border-accent/30 bg-accent/10 px-4 py-3"
        >
          <div className="flex items-start gap-2">
            <Info size={16} className="mt-0.5 shrink-0 text-accent" aria-hidden="true" />
            <div>
              <div className="mb-1 text-sm font-semibold text-blue-100">
                When to use Engineering / change system
              </div>
              <p className="text-sm leading-relaxed text-muted">
                Use this role when a system primarily reflects technical change activity,
                release work, or engineering backlog, not business workflow execution.
              </p>
            </div>
          </div>
        </div>
      )}

      <div className="rounded-xl border border-border bg-panel p-4 shadow-sm">
        <div className="flex flex-col gap-4 md:flex-row md:items-center md:justify-between">
          <Button variant="tertiary" onClick={() => goTo(2)} className="gap-2">
            <ArrowLeft size={16} strokeWidth={2.2} aria-hidden="true" />
            Back
          </Button>

          <Button variant="tertiary" onClick={() => goTo(4)} className="gap-2">
            Continue to discovery plan
            <MoveRight size={16} strokeWidth={2.2} aria-hidden="true" />
          </Button>
        </div>
      </div>
    </div>
  );
}
