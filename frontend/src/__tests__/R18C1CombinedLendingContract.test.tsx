/**
 * R18-C1 T6 - browser-side contract for registry-driven Lending Stack Builder.
 *
 * The harness composes the real focus picker, setup-state hook, weighting UI,
 * first-run guide, launch-payload builder, and post-launch provenance notice.
 */
import React, { useMemo, useState } from 'react';
import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, within } from '@testing-library/react';

import LendingFirstRunGuide, {
  type LendingGuideLaunchState,
} from '../components/stack_builder/LendingFirstRunGuide';
import { useSetupState } from '../components/stack_builder';
import TemplateRunNotice from '../components/discovery_run/TemplateRunNotice';
import DiscoveryFocusPage from '../pages/DiscoveryFocusPage';
import SourceWeightingPage from '../pages/SourceWeightingPage';
import { buildStackBuilderLaunchPayload } from '../pages/StackBuilderPage';
import type { DiscoveryRun } from '../types/discoveryRun';
import type {
  IndustryListItem,
  TemplateListItem,
} from '../types/stack_builder';

const LENDING: TemplateListItem = {
  template_id: 'commercial_lending',
  label: 'Commercial lending from registry',
  description: 'Registry-owned lending configuration used by this contract.',
  suggested_systems: ['salesforce_ncino', 'jira', 'servicenow', 'confluence'],
  suggested_roles: {
    salesforce_ncino: 'system_of_record',
    jira: 'workflow_system',
    servicenow: 'workflow_system',
    confluence: 'documentation_system',
  },
  focus_defaults: {
    focus_id: 'approvals_compliance',
    emphasis: ['approvals', 'compliance_risk'],
  },
  pack_id: 'ncino',
  detector_emphasis: [
    'COVENANT_TRACKING_GAP',
    'APPROVAL_BOTTLENECK',
  ],
  terminology: {
    customer: 'borrower',
    account: 'facility',
    obligation: 'covenant',
  },
  metadata: { industry_id: 'financial_services', source: 'contract_registry' },
};

const INSURANCE_FIXTURE: TemplateListItem = {
  template_id: 'insurance_contract_fixture',
  label: 'Insurance template from fixture config',
  description: 'A template added by configuration only.',
  suggested_systems: ['salesforce_sc'],
  suggested_roles: { salesforce_sc: 'system_of_record' },
  focus_defaults: {
    focus_id: 'member_customer_service',
    emphasis: ['service_casework'],
  },
  pack_id: 'service_cloud',
  detector_emphasis: [],
  terminology: { customer: 'policyholder' },
  metadata: { source: 'contract_fixture' },
};

const FINANCIAL_SERVICES: IndustryListItem = {
  industry_id: 'financial_services',
  label: 'Financial services from registry',
  pack_hints: ['ncino'],
  recommended_systems: ['jira', 'servicenow', 'confluence'],
};

interface HarnessProps {
  industries: IndustryListItem[];
  templates: TemplateListItem[];
  registryError?: string | null;
}

function ContractHarness({
  industries,
  templates,
  registryError = null,
}: HarnessProps) {
  const setupState = useSetupState();
  const [launchState, setLaunchState] = useState<LendingGuideLaunchState>('setup');
  const selectedTemplate = templates.find(
    item => item.template_id === setupState.state.templateId,
  );
  const packId = selectedTemplate?.pack_id ?? 'service_cloud';
  const payload = useMemo(
    () => buildStackBuilderLaunchPayload(setupState.state, packId, 'contract-org'),
    [setupState.state, packId],
  );

  return (
    <div>
      <DiscoveryFocusPage
        setupState={setupState}
        industries={industries}
        templates={templates}
        registryLoading={false}
        registryError={registryError}
        onRetryRegistry={vi.fn()}
        fetchSystemDefaults={vi.fn(async () => [])}
      />

      {selectedTemplate && (
        <LendingFirstRunGuide
          template={selectedTemplate}
          state={setupState.state}
          packId={packId}
          launchState={launchState}
        />
      )}

      <SourceWeightingPage setupState={setupState} />

      <button type="button" onClick={() => setupState.toggleSystem('jira')}>
        Toggle Jira in configured systems
      </button>
      <button type="button" onClick={() => setLaunchState('launching')}>
        Launch configured template
      </button>

      <output data-testid="launch-payload">{JSON.stringify(payload)}</output>
    </div>
  );
}

describe('R18-C1 T6 - combined registry and first-run guide contract', () => {
  it('renders an arbitrary industry and template supplied by registry data only', () => {
    const fixtureIndustry: IndustryListItem = {
      industry_id: 'insurance_contract_fixture',
      label: 'Insurance industry from fixture config',
      pack_hints: ['service_cloud'],
      recommended_systems: ['salesforce_sc'],
    };
    render(
      <ContractHarness
        industries={[fixtureIndustry]}
        templates={[INSURANCE_FIXTURE]}
      />,
    );

    expect(
      screen.getByRole('checkbox', { name: fixtureIndustry.label }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole('checkbox', { name: INSURANCE_FIXTURE.label }),
    ).toBeInTheDocument();
    expect(screen.queryByText(LENDING.label)).not.toBeInTheDocument();
  });

  it('keeps template defaults editable and feeds the live guide and launch payload', () => {
    render(
      <ContractHarness
        industries={[FINANCIAL_SERVICES]}
        templates={[LENDING, INSURANCE_FIXTURE]}
      />,
    );

    fireEvent.click(screen.getByRole('checkbox', { name: LENDING.label }));

    const guideHeading = screen.getByRole('heading', {
      name: `${LENDING.label} setup guide`,
    });
    const guide = guideHeading.closest('section');
    expect(guide).not.toBeNull();
    const guideQueries = within(guide as HTMLElement);

    expect(guideQueries.getByText(LENDING.description)).toBeInTheDocument();
    expect(guideQueries.getByText('nCino / Salesforce')).toBeInTheDocument();
    expect(guideQueries.getByText('Jira')).toBeInTheDocument();
    expect(guideQueries.getByText('Covenant Tracking Gap')).toBeInTheDocument();
    expect(guideQueries.getByText('Approvals Compliance')).toBeInTheDocument();

    // Edit the focus through the real focus card.
    const coreFocus = screen.getByText('Core operations').closest('[role="radio"]');
    expect(coreFocus).not.toBeNull();
    fireEvent.click(coreFocus as HTMLElement);
    expect(guideQueries.getByText('Core Operations')).toBeInTheDocument();

    // Edit nCino's role through the real weighting card. Template defaults are
    // intentionally unconfirmed, so the user remains in control before launch.
    fireEvent.click(
      screen.getByRole('button', { name: /Salesforce - nCino/i }),
    );
    fireEvent.click(screen.getByRole('checkbox', { name: 'Documentation system' }));
    const ncinoGuideItem = guideQueries
      .getByText('nCino / Salesforce')
      .closest('[role="listitem"]');
    expect(ncinoGuideItem).not.toBeNull();
    expect(
      within(ncinoGuideItem as HTMLElement).getByText(
        /Documentation source - Policies, procedures/,
      ),
    ).toBeInTheDocument();

    // Removing a template-provided system also updates the guide immediately.
    fireEvent.click(
      screen.getByRole('button', { name: 'Toggle Jira in configured systems' }),
    );
    expect(guideQueries.queryByText('Jira')).not.toBeInTheDocument();

    fireEvent.click(
      screen.getByRole('button', { name: 'Launch configured template' }),
    );
    expect(
      screen.getByRole('heading', { name: `Launching ${LENDING.label}` }),
    ).toBeInTheDocument();

    const payload = JSON.parse(screen.getByTestId('launch-payload').textContent ?? '{}');
    expect(payload.template_id).toBe('commercial_lending');
    expect(payload.pack_id).toBe('ncino');
    expect(payload.focus_id).toBe('core_operations');
    expect(payload.selected_system_ids).not.toContain('jira');
    expect(payload.weightings.salesforce_ncino.role).toBe('documentation_system');
    expect(payload.weightings.salesforce_ncino.confirmed).toBe(false);
  });

  it('explains the active template and preserved edits while its run is launching', () => {
    const run = {
      id: 'run-r18-c1-t6',
      status: 'running',
      templateId: 'commercial_lending',
      packId: 'ncino',
      focusId: 'core_operations',
      selectedSystemIds: ['salesforce_ncino', 'confluence', 'slack'],
      templateProvenance: {
        applied: true,
        template_id: 'commercial_lending',
        untouched: false,
        edited_fields: ['focus_id', 'selected_system_ids', 'roles'],
      },
    } as DiscoveryRun;

    render(<TemplateRunNotice run={run} computing />);

    expect(screen.getByText('Using the Commercial Lending template')).toBeInTheDocument();
    expect(screen.getByText(/Discovery is running with 3 configured systems/)).toBeInTheDocument();
    expect(screen.getByText(/Focus Id, Selected System Ids, Roles/)).toBeInTheDocument();
  });
});
