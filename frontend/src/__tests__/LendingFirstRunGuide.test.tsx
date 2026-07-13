import React from 'react';
import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import LendingFirstRunGuide from '../components/stack_builder/LendingFirstRunGuide';
import TemplateRunNotice from '../components/discovery_run/TemplateRunNotice';
import type { SetupState, TemplateListItem } from '../types/stack_builder';
import type { DiscoveryRun } from '../types/discoveryRun';

const template: TemplateListItem = {
  template_id: 'commercial_lending',
  label: 'Commercial lending',
  description: 'Registry-provided lending setup.',
  suggested_systems: ['salesforce_ncino', 'jira'],
  suggested_roles: {
    salesforce_ncino: 'system_of_record',
    jira: 'workflow_system',
  },
  focus_defaults: {
    focus_id: 'approvals_compliance',
    emphasis: ['approvals', 'compliance_risk'],
  },
  pack_id: 'ncino',
  detector_emphasis: ['COVENANT_TRACKING_GAP', 'APPROVAL_BOTTLENECK'],
  terminology: { customer: 'borrower', obligation: 'covenant' },
  metadata: { source: 'test_registry' },
};

function setupState(overrides: Partial<SetupState> = {}): SetupState {
  return {
    focusId: 'approvals_compliance',
    industryId: 'financial_services',
    templateId: 'commercial_lending',
    templatePreselectedIds: ['salesforce_ncino', 'jira'],
    selectedSystemIds: ['salesforce_ncino', 'jira'],
    selectedSalesforceClouds: [],
    weightings: {
      salesforce_ncino: {
        systemId: 'salesforce_ncino',
        role: 'system_of_record',
        priority: 'primary',
        workflowFocus: ['approvals'],
        confirmed: true,
      },
      jira: {
        systemId: 'jira',
        role: 'workflow_system',
        priority: 'secondary',
        workflowFocus: ['backlog_work_queues'],
        confirmed: false,
      },
    },
    currentStep: 3,
    ...overrides,
  };
}

describe('R18-C1 T5 - Commercial Lending first-run guide', () => {
  it('uses registry template data and the live configured systems and roles', () => {
    render(
      <LendingFirstRunGuide
        template={template}
        state={setupState()}
        packId="ncino"
        launchState="setup"
      />,
    );

    expect(screen.getByText('Registry-provided lending setup.')).toBeInTheDocument();
    expect(screen.getByText('nCino / Salesforce')).toBeInTheDocument();
    expect(screen.getByText('Jira')).toBeInTheDocument();
    expect(screen.getByText(/System of record - Primary business records/)).toBeInTheDocument();
    expect(screen.getByText(/Workflow system - Queues, approvals/)).toBeInTheDocument();
    expect(screen.getByText('Covenant Tracking Gap')).toBeInTheDocument();
    expect(screen.getByText('1/2 roles confirmed')).toBeInTheDocument();
  });

  it('updates when the user changes systems and roles before launch', () => {
    const view = render(
      <LendingFirstRunGuide
        template={template}
        state={setupState()}
        packId="ncino"
        launchState="setup"
      />,
    );

    const changed = setupState({
      selectedSystemIds: ['salesforce_ncino', 'slack'],
      weightings: {
        salesforce_ncino: {
          systemId: 'salesforce_ncino',
          role: 'documentation_system',
          priority: 'secondary',
          workflowFocus: ['documents_knowledge'],
          confirmed: true,
        },
        slack: {
          systemId: 'slack',
          role: 'operational_signal_source',
          priority: 'secondary',
          workflowFocus: ['communications'],
          confirmed: true,
        },
      },
    });

    view.rerender(
      <LendingFirstRunGuide
        template={template}
        state={changed}
        packId="ncino"
        launchState="ready"
      />,
    );

    expect(screen.queryByText('Jira')).not.toBeInTheDocument();
    expect(screen.getByText('Slack')).toBeInTheDocument();
    expect(screen.getByText(/Documentation source - Policies/)).toBeInTheDocument();
    expect(screen.getByText('2/2 roles confirmed')).toBeInTheDocument();
    expect(screen.getByText('Confirm your lending discovery setup')).toBeInTheDocument();
  });

  it('shows the actual non-template pack instead of stale lending detector claims', () => {
    render(
      <LendingFirstRunGuide
        template={template}
        state={setupState()}
        packId="service_cloud"
        launchState="ready"
      />,
    );

    expect(screen.getByText(/active analysis pack is Service Cloud/)).toBeInTheDocument();
    expect(screen.queryByText('Covenant Tracking Gap')).not.toBeInTheDocument();
  });

  it('makes launch progress and failure explicit', () => {
    const view = render(
      <LendingFirstRunGuide
        template={template}
        state={setupState()}
        packId="ncino"
        launchState="launching"
      />,
    );
    expect(screen.getByText('Launching Commercial lending')).toBeInTheDocument();

    view.rerender(
      <LendingFirstRunGuide
        template={template}
        state={setupState()}
        packId="ncino"
        launchState="launch_error"
      />,
    );
    expect(screen.getByText('The lending run was not created')).toBeInTheDocument();
  });
});

function run(overrides: Partial<DiscoveryRun> = {}): DiscoveryRun {
  return {
    id: 'run-12345678',
    status: 'running',
    startedAt: '2026-07-13T10:00:00Z',
    updatedAt: '2026-07-13T10:00:00Z',
    inputs: {
      connectedSources: [],
      uploadedFiles: [],
      sampleWorkspaceEnabled: false,
    },
    progress: { percent: 10, currentStepId: 'sf_crm', etaSeconds: 30 },
    steps: [],
    summary: {
      appsDetected: 0,
      workflowsInferred: 0,
      opportunitiesFound: 0,
      confidence: 'LOW',
      warnings: 0,
    },
    templateId: 'commercial_lending',
    packId: 'ncino',
    focusId: 'approvals_compliance',
    selectedSystemIds: ['salesforce_ncino', 'slack'],
    templateProvenance: {
      template_id: 'commercial_lending',
      applied: true,
      untouched: false,
      edited_fields: ['selected_system_ids', 'roles'],
    },
    ...overrides,
  };
}

describe('R18-C1 T5 - launched template provenance guidance', () => {
  it('shows the configured template, effective run config, and preserved edits while running', () => {
    render(<TemplateRunNotice run={run()} computing />);

    expect(screen.getByText('Using the Commercial Lending template')).toBeInTheDocument();
    expect(screen.getByText(/Discovery is running with 2 configured systems/)).toBeInTheDocument();
    expect(screen.getByText(/nCino lending pack/)).toBeInTheDocument();
    expect(screen.getByText(/Selected System Ids, Roles/)).toBeInTheDocument();
  });

  it('reflects the completed run state and untouched provenance', () => {
    render(
      <TemplateRunNotice
        run={run({
          status: 'complete',
          templateProvenance: {
            template_id: 'commercial_lending',
            applied: true,
            untouched: true,
            edited_fields: [],
          },
        })}
        computing={false}
      />,
    );

    expect(screen.getByText('Commercial Lending template run')).toBeInTheDocument();
    expect(screen.getByText(/This run used 2 configured systems/)).toBeInTheDocument();
    expect(screen.getByText(/defaults were used without changes/)).toBeInTheDocument();
  });
});
