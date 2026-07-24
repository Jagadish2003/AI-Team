import React from 'react';
import {
  AlertCircle,
  CheckCircle2,
  Landmark,
  Loader2,
  PlayCircle,
  Settings2,
} from 'lucide-react';
import type {
  SetupState,
  SystemRole,
  TemplateListItem,
} from '../../types/stack_builder';

export type LendingGuideLaunchState =
  | 'setup'
  | 'ready'
  | 'launching'
  | 'launch_error';

const SYSTEM_NAMES: Record<string, string> = {
  salesforce: 'Salesforce',
  salesforce_ncino: 'nCino / Salesforce',
  salesforce_fsc: 'Salesforce Financial Services Cloud',
  jira: 'Jira',
  servicenow: 'ServiceNow',
  slack: 'Slack',
  teams: 'Microsoft Teams',
  confluence: 'Confluence',
  sharepoint: 'SharePoint',
  notion: 'Notion',
};

const ROLE_DETAILS: Record<SystemRole, { label: string; meaning: string }> = {
  system_of_record: {
    label: 'System of record',
    meaning: 'Primary business records and lifecycle data.',
  },
  workflow_system: {
    label: 'Workflow system',
    meaning: 'Queues, approvals, handoffs, and work progression.',
  },
  operational_signal_source: {
    label: 'Supporting signal',
    meaning: 'Activity signals that corroborate how work happens.',
  },
  documentation_system: {
    label: 'Documentation source',
    meaning: 'Policies, procedures, and decision context.',
  },
  engineering_change_system: {
    label: 'Change system',
    meaning: 'Change, release, and implementation evidence.',
  },
};

function humanise(value: string): string {
  if (value === 'ncino') return 'nCino lending';
  return value
    .split(/[_-]+/)
    .filter(Boolean)
    .map(word => word.charAt(0).toUpperCase() + word.slice(1).toLowerCase())
    .join(' ');
}

function systemName(id: string): string {
  return SYSTEM_NAMES[id] ?? humanise(id);
}

function statusCopy(status: LendingGuideLaunchState, templateLabel: string) {
  switch (status) {
    case 'launching':
      return {
        title: `Launching ${templateLabel}`,
        body: 'Creating the run with the systems, roles, focus, and analysis pack shown below.',
        Icon: Loader2,
      };
    case 'launch_error':
      return {
        title: 'The lending run was not created',
        body: 'Your configuration is still here. Review it and try launching again.',
        Icon: AlertCircle,
      };
    case 'ready':
      return {
        title: 'Confirm your lending discovery setup',
        body: 'This is the configuration AgentIQ will use when you start discovery.',
        Icon: PlayCircle,
      };
    default:
      return {
        title: `${templateLabel} setup guide`,
        body: 'Use this guide to confirm what the template configured before launch.',
        Icon: Landmark,
      };
  }
}

interface Props {
  template: TemplateListItem;
  state: SetupState;
  packId: string;
  launchState: LendingGuideLaunchState;
}

/**
 * R18-C1 T5 first-run guidance. Every configurable value is read from the
 * selected registry template or live SetupState; the component owns no lending
 * system/role defaults that could drift from what the run will actually use.
 */
export default function LendingFirstRunGuide({
  template,
  state,
  packId,
  launchState,
}: Props) {
  if (template.template_id !== 'commercial_lending') return null;

  const copy = statusCopy(launchState, template.label);
  const StatusIcon = copy.Icon;
  const configuredSystems = state.selectedSystemIds.map(id => ({
    id,
    weighting: state.weightings[id],
  }));
  const confirmedRoles = configuredSystems.filter(item => item.weighting?.confirmed).length;
  const usingTemplatePack = packId === template.pack_id;
  const focusLabel = state.focusId ? humanise(state.focusId) : 'Not selected';

  return (
    <section
      aria-labelledby="lending-guide-title"
      aria-live={launchState === 'launching' || launchState === 'launch_error' ? 'polite' : undefined}
      className="overflow-hidden rounded-xl border border-accent/30 bg-gradient-to-br from-accent/10 via-panel to-panel shadow-sm"
    >
      <div className="border-b border-accent/20 px-5 py-4">
        <div className="flex items-start gap-3">
          <StatusIcon
            size={20}
            className={`mt-0.5 flex-shrink-0 text-accent ${launchState === 'launching' ? 'animate-spin' : ''}`}
            aria-hidden="true"
          />
          <div>
            <p className="text-xs font-semibold uppercase tracking-[0.14em] text-accent">
              First-run guide
            </p>
            <h2 id="lending-guide-title" className="mt-1 text-base font-semibold text-text">
              {copy.title}
            </h2>
            <p className="mt-1 text-sm leading-relaxed text-muted">{copy.body}</p>
            <p className="mt-2 text-xs leading-relaxed text-muted">{template.description}</p>
          </div>
        </div>
      </div>

      <div className="grid gap-4 p-5 lg:grid-cols-[minmax(0,1.35fr)_minmax(240px,0.65fr)]">
        <div>
          <div className="mb-3 flex items-center justify-between gap-3">
            <h3 className="text-sm font-semibold text-text">Configured systems and roles</h3>
            <span className="text-xs text-muted">
              {confirmedRoles}/{configuredSystems.length} roles confirmed
            </span>
          </div>

          {configuredSystems.length === 0 ? (
            <div className="rounded-lg border border-dashed border-border px-4 py-3 text-sm text-muted">
              No systems are selected. Add at least one source before launch.
            </div>
          ) : (
            <div role="list" className="space-y-2">
              {configuredSystems.map(({ id, weighting }) => {
                const role = weighting?.role;
                const details = role ? ROLE_DETAILS[role] : null;
                return (
                  <div
                    key={id}
                    role="listitem"
                    className="rounded-lg border border-border/80 bg-bg/15 px-3 py-2.5"
                  >
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <span className="text-sm font-medium text-text">{systemName(id)}</span>
                      <span className={`inline-flex items-center gap-1 text-xs ${weighting?.confirmed ? 'text-emerald-300' : 'text-amber-300'}`}>
                        {weighting?.confirmed ? <CheckCircle2 size={13} aria-hidden="true" /> : <Settings2 size={13} aria-hidden="true" />}
                        {weighting?.confirmed ? 'Confirmed' : 'Review before launch'}
                      </span>
                    </div>
                    <p className="mt-1 text-xs leading-relaxed text-muted">
                      {details ? `${details.label} - ${details.meaning}` : 'Role not configured yet.'}
                    </p>
                  </div>
                );
              })}
            </div>
          )}
        </div>

        <div className="space-y-3">
          <div className="rounded-lg border border-border/80 bg-bg/15 p-3">
            <h3 className="text-sm font-semibold text-text">What AgentIQ will analyze</h3>
            <dl className="mt-2 space-y-2 text-xs">
              <div>
                <dt className="text-muted">Discovery focus</dt>
                <dd className="mt-0.5 font-medium text-text">{focusLabel}</dd>
              </div>
              <div>
                <dt className="text-muted">Analysis pack</dt>
                <dd className="mt-0.5 font-medium text-text">{humanise(packId)}</dd>
              </div>
            </dl>
          </div>

          {usingTemplatePack && template.detector_emphasis.length > 0 ? (
            <div className="rounded-lg border border-border/80 bg-bg/15 p-3">
              <h3 className="text-sm font-semibold text-text">Lending patterns in scope</h3>
              <ul className="mt-2 space-y-1.5 text-xs leading-relaxed text-muted">
                {template.detector_emphasis.map(detector => (
                  <li key={detector} className="flex gap-2">
                    <span className="text-accent" aria-hidden="true">-</span>
                    <span>{humanise(detector)}</span>
                  </li>
                ))}
              </ul>
            </div>
          ) : (
            <div className="rounded-lg border border-amber-400/30 bg-amber-500/10 p-3 text-xs leading-relaxed text-amber-100">
              The active analysis pack is {humanise(packId)}, not the template default of{' '}
              {humanise(template.pack_id)}. The run will use the active pack shown here.
            </div>
          )}

          <p className="text-xs leading-relaxed text-muted">
            The run record will preserve the selected template and any configuration changes.
          </p>
        </div>
      </div>
    </section>
  );
}
